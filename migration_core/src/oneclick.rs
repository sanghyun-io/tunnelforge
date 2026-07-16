use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde::{de, Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

use mysql::prelude::Queryable;
use crate::*;
use crate::schema::{error_event, inspect_mysql_with_conn};

pub(crate) const ONECLICK_PLAN_VERSION: u32 = 1;
pub(crate) const ONECLICK_APPROVAL_VERSION: u32 = 1;
pub(crate) const ONECLICK_PROFILE_VERSION: u32 = 1;
pub(crate) const ACTION_FACTS_VERSION: u32 = 1;
const ONECLICK_SNAPSHOT_VERSION: u32 = 1;
pub(crate) const ONECLICK_EXACT_PLAN_ENABLED: bool = false;
pub(crate) const ONECLICK_STRONG_FENCE_PROVEN: bool = false;

const ACTION_FACTS_HASH_DOMAIN: &[u8] = b"tunnelforge.oneclick.action-facts.v1\0";
const SNAPSHOT_HASH_DOMAIN: &[u8] = b"tunnelforge.oneclick.snapshot.v1\0";
const PLAN_HASH_DOMAIN: &[u8] = b"tunnelforge.oneclick.plan.v1\0";
const LOCK_KEY_HASH_DOMAIN: &[u8] = b"tunnelforge.oneclick.lock.v1\0";
const ONECLICK_LOCK_TIMEOUT_SECONDS: u32 = 10;

pub(crate) fn oneclick_apply_enabled(
    exact_plan_enabled: bool,
    strong_fence_proven: bool,
) -> bool {
    exact_plan_enabled && strong_fence_proven
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OneClickContractError {
    code: &'static str,
    message: &'static str,
    applied_ordinals: Vec<u32>,
    indeterminate_ordinal: Option<u32>,
    outcome_indeterminate: bool,
    lock_release_failed: bool,
}

impl OneClickContractError {
    fn new(code: &'static str, message: &'static str) -> Self {
        Self {
            code,
            message,
            applied_ordinals: Vec::new(),
            indeterminate_ordinal: None,
            outcome_indeterminate: false,
            lock_release_failed: false,
        }
    }

    pub(crate) fn code(&self) -> &'static str {
        self.code
    }

    pub(crate) fn applied_ordinals(&self) -> &[u32] {
        &self.applied_ordinals
    }

    pub(crate) fn lock_release_failed(&self) -> bool {
        self.lock_release_failed
    }

    pub(crate) fn indeterminate_ordinal(&self) -> Option<u32> {
        self.indeterminate_ordinal
    }

    pub(crate) fn outcome_indeterminate(&self) -> bool {
        self.outcome_indeterminate
    }

    fn with_applied_ordinals(mut self, applied_ordinals: Vec<u32>) -> Self {
        self.applied_ordinals = applied_ordinals;
        self
    }

    fn with_indeterminate_ordinal(mut self, ordinal: u32) -> Self {
        self.indeterminate_ordinal = Some(ordinal);
        self.outcome_indeterminate = true;
        self
    }

    fn with_lock_release_failed(mut self) -> Self {
        self.lock_release_failed = true;
        self
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickRoute {
    pub(crate) host: String,
    pub(crate) port: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickTargetIdentity {
    pub(crate) engine: String,
    pub(crate) route: OneClickRoute,
    pub(crate) server_uuid: String,
    pub(crate) authenticated_user: String,
    pub(crate) schema: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickRemediationProfile {
    pub(crate) profile_version: u32,
    pub(crate) profile_id: String,
    pub(crate) target_charset: String,
    pub(crate) target_collation: String,
}

pub(crate) fn fixed_oneclick_profile() -> OneClickRemediationProfile {
    OneClickRemediationProfile {
        profile_version: ONECLICK_PROFILE_VERSION,
        profile_id: "mysql-utf8mb4-0900-v1".to_string(),
        target_charset: "utf8mb4".to_string(),
        target_collation: "utf8mb4_0900_ai_ci".to_string(),
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub(crate) enum OneClickActionType {
    EngineInnodb,
    CharsetFkSafe,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ColumnDefaultFact {
    Absent,
    Null,
    Literal(String),
    Expression(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct LiteralDefaultWire {
    literal: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ExpressionDefaultWire {
    expression: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
enum ColumnDefaultWire {
    Keyword(String),
    Literal(LiteralDefaultWire),
    Expression(ExpressionDefaultWire),
}

impl Serialize for ColumnDefaultFact {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let wire = match self {
            Self::Absent => ColumnDefaultWire::Keyword("absent".to_string()),
            Self::Null => ColumnDefaultWire::Keyword("null".to_string()),
            Self::Literal(literal) => ColumnDefaultWire::Literal(LiteralDefaultWire {
                literal: literal.clone(),
            }),
            Self::Expression(expression) => {
                ColumnDefaultWire::Expression(ExpressionDefaultWire {
                    expression: expression.clone(),
                })
            }
        };
        wire.serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for ColumnDefaultFact {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        match ColumnDefaultWire::deserialize(deserializer)? {
            ColumnDefaultWire::Keyword(keyword) if keyword == "absent" => Ok(Self::Absent),
            ColumnDefaultWire::Keyword(keyword) if keyword == "null" => Ok(Self::Null),
            ColumnDefaultWire::Keyword(_) => Err(de::Error::custom(
                "column default keyword must be absent or null",
            )),
            ColumnDefaultWire::Literal(value) => Ok(Self::Literal(value.literal)),
            ColumnDefaultWire::Expression(value) => Ok(Self::Expression(value.expression)),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionColumnFact {
    pub(crate) ordinal_position: u32,
    pub(crate) name: String,
    pub(crate) column_type: String,
    pub(crate) nullable: bool,
    pub(crate) default: ColumnDefaultFact,
    pub(crate) charset: Option<String>,
    pub(crate) collation: Option<String>,
    pub(crate) generated_expression: Option<String>,
    pub(crate) generated_stored: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionIndexColumnFact {
    pub(crate) ordinal_position: u32,
    pub(crate) column_name: Option<String>,
    pub(crate) expression: Option<String>,
    pub(crate) prefix_length: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionIndexFact {
    pub(crate) name: String,
    pub(crate) unique: bool,
    pub(crate) index_type: String,
    pub(crate) visible: bool,
    pub(crate) columns: Vec<ActionIndexColumnFact>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionTableDefinitionFact {
    pub(crate) schema: String,
    pub(crate) table: String,
    pub(crate) engine: Option<String>,
    pub(crate) charset: Option<String>,
    pub(crate) collation: Option<String>,
    pub(crate) columns: Vec<ActionColumnFact>,
    pub(crate) indexes: Vec<ActionIndexFact>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionForeignKeyColumnFact {
    pub(crate) ordinal_position: u32,
    pub(crate) column_name: String,
    pub(crate) referenced_column_name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionForeignKeyFact {
    pub(crate) constraint_schema: String,
    pub(crate) constraint_name: String,
    pub(crate) table_schema: String,
    pub(crate) table_name: String,
    pub(crate) referenced_table_schema: String,
    pub(crate) referenced_table_name: String,
    pub(crate) match_option: String,
    pub(crate) update_rule: String,
    pub(crate) delete_rule: String,
    pub(crate) columns: Vec<ActionForeignKeyColumnFact>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionFactsDocument {
    pub(crate) action_facts_version: u32,
    pub(crate) action_type: OneClickActionType,
    pub(crate) tables: Vec<ActionTableDefinitionFact>,
    pub(crate) foreign_keys: Vec<ActionForeignKeyFact>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickActionStateExpectation {
    pub(crate) facts: ActionFactsDocument,
    pub(crate) facts_hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickApplyAction {
    pub(crate) ordinal: u32,
    pub(crate) action_type: OneClickActionType,
    pub(crate) issue_type: String,
    pub(crate) strategy: String,
    pub(crate) schema: String,
    pub(crate) tables: Vec<String>,
    pub(crate) sql: String,
    pub(crate) rollback_sql: Option<String>,
    pub(crate) target_charset: Option<String>,
    pub(crate) target_collation: Option<String>,
    pub(crate) expected_pre_facts: OneClickActionStateExpectation,
    pub(crate) expected_post_facts: OneClickActionStateExpectation,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickInspectionFact {
    pub(crate) issue_type: String,
    pub(crate) severity: String,
    pub(crate) object_kind: String,
    pub(crate) schema: String,
    pub(crate) table: Option<String>,
    pub(crate) column: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickSnapshotDocument {
    pub(crate) snapshot_version: u32,
    pub(crate) schema: String,
    pub(crate) inspection_facts: Vec<OneClickInspectionFact>,
    pub(crate) table_definitions: Vec<ActionTableDefinitionFact>,
    pub(crate) foreign_keys: Vec<ActionForeignKeyFact>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickPlanEnvelope {
    pub(crate) plan_version: u32,
    pub(crate) target_identity: OneClickTargetIdentity,
    pub(crate) remediation_profile: OneClickRemediationProfile,
    pub(crate) snapshot: OneClickSnapshotDocument,
    pub(crate) snapshot_hash: String,
    pub(crate) actions: Vec<OneClickApplyAction>,
    pub(crate) plan_hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct OneClickApprovalArtifact {
    pub(crate) approval_version: u32,
    pub(crate) plan_version: u32,
    pub(crate) target_identity: OneClickTargetIdentity,
    pub(crate) remediation_profile: OneClickRemediationProfile,
    pub(crate) snapshot_hash: String,
    pub(crate) plan_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OneClickApplyOutcome {
    pub(crate) applied_ordinals: Vec<u32>,
}

#[derive(Serialize)]
struct OneClickPlanHashDocument<'a> {
    plan_version: u32,
    target_identity: &'a OneClickTargetIdentity,
    remediation_profile: &'a OneClickRemediationProfile,
    snapshot_hash: &'a str,
    actions: &'a [OneClickApplyAction],
}

#[derive(Debug)]
pub(crate) struct ValidatedOneClickPlanRequest {
    pub(crate) endpoint: Endpoint,
    pub(crate) schema: String,
}

#[derive(Debug, Clone)]
pub(crate) struct ValidatedOneClickApplyRequest {
    pub(crate) endpoint: Endpoint,
    pub(crate) schema: String,
    pub(crate) approval: OneClickApprovalArtifact,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OneClickPlanRequestWire {
    connection: OneClickEndpointWire,
    schema: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OneClickEndpointWire {
    engine: String,
    host: String,
    port: u16,
    user: String,
    password: String,
    #[serde(default)]
    database: Option<String>,
    #[serde(default)]
    schema: Option<String>,
}

pub(crate) fn normalize_oneclick_schema(
    schema: &str,
) -> Result<String, OneClickContractError> {
    if schema.is_empty()
        || schema.contains('\0')
        || schema.trim().len() != schema.len()
    {
        return Err(OneClickContractError::new(
            "oneclick_schema_invalid",
            "One-Click schema is invalid.",
        ));
    }
    Ok(schema.to_string())
}

pub(crate) fn parse_oneclick_plan_request(
    request: &Request,
) -> Result<ValidatedOneClickPlanRequest, OneClickContractError> {
    const ROOT_KEYS: &[&str] = &["connection", "schema"];
    const ENDPOINT_KEYS: &[&str] = &[
        "engine", "host", "port", "user", "password", "database", "schema",
    ];
    let root = request.payload.as_object().ok_or_else(|| {
        OneClickContractError::new(
            "oneclick_plan_payload_prohibited",
            "One-Click plan payload is prohibited.",
        )
    })?;
    if root.keys().any(|key| !ROOT_KEYS.contains(&key.as_str())) {
        return Err(OneClickContractError::new(
            "oneclick_plan_payload_prohibited",
            "One-Click plan payload is prohibited.",
        ));
    }
    let connection = root
        .get("connection")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            OneClickContractError::new(
                "oneclick_plan_payload_prohibited",
                "One-Click plan payload is prohibited.",
            )
        })?;
    if connection
        .keys()
        .any(|key| !ENDPOINT_KEYS.contains(&key.as_str()))
    {
        return Err(OneClickContractError::new(
            "oneclick_plan_payload_prohibited",
            "One-Click plan payload is prohibited.",
        ));
    }
    let schema = root
        .get("schema")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            OneClickContractError::new(
                "oneclick_plan_payload_prohibited",
                "One-Click plan payload is prohibited.",
            )
        })
        .and_then(normalize_oneclick_schema)?;
    for key in ["database", "schema"] {
        if connection
            .get(key)
            .is_some_and(|value| value.as_str() != Some(schema.as_str()))
        {
            return Err(OneClickContractError::new(
                "oneclick_schema_mismatch",
                "One-Click schema does not match the connection schema.",
            ));
        }
    }

    let wire: OneClickPlanRequestWire =
        serde_json::from_value(request.payload.clone()).map_err(|_| {
            OneClickContractError::new(
                "oneclick_plan_payload_prohibited",
                "One-Click plan payload is prohibited.",
            )
        })?;
    if wire.schema != schema
        || wire
            .connection
            .database
            .as_deref()
            .is_some_and(|nested| nested != schema)
        || wire
            .connection
            .schema
            .as_deref()
            .is_some_and(|nested| nested != schema)
    {
        return Err(OneClickContractError::new(
            "oneclick_schema_mismatch",
            "One-Click schema does not match the connection schema.",
        ));
    }
    if wire.connection.engine != "mysql"
        || wire.connection.host.trim().is_empty()
        || wire.connection.user.trim().is_empty()
    {
        return Err(OneClickContractError::new(
            "oneclick_plan_payload_prohibited",
            "One-Click plan endpoint is invalid.",
        ));
    }
    Ok(ValidatedOneClickPlanRequest {
        endpoint: Endpoint {
            engine: wire.connection.engine,
            host: wire.connection.host,
            port: wire.connection.port,
            user: wire.connection.user,
            password: wire.connection.password,
            database: schema.clone(),
            schema: Some(schema.clone()),
        },
        schema,
    })
}

fn object_has_only_keys(object: &serde_json::Map<String, Value>, allowed: &[&str]) -> bool {
    object.keys().all(|key| allowed.contains(&key.as_str()))
}

fn is_sha256_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(crate) fn parse_oneclick_apply_request(
    request: &Request,
) -> Result<ValidatedOneClickApplyRequest, OneClickContractError> {
    const ROOT_KEYS: &[&str] = &[
        "connection",
        "schema",
        "dry_run",
        "backup_confirmed",
        "approval",
    ];
    const ENDPOINT_KEYS: &[&str] = &[
        "engine", "host", "port", "user", "password", "database", "schema",
    ];
    const APPROVAL_KEYS: &[&str] = &[
        "approval_version",
        "plan_version",
        "target_identity",
        "remediation_profile",
        "snapshot_hash",
        "plan_hash",
    ];
    const IDENTITY_KEYS: &[&str] = &[
        "engine",
        "route",
        "server_uuid",
        "authenticated_user",
        "schema",
    ];
    const ROUTE_KEYS: &[&str] = &["host", "port"];
    const PROFILE_KEYS: &[&str] = &[
        "profile_version",
        "profile_id",
        "target_charset",
        "target_collation",
    ];

    let prohibited = || {
        OneClickContractError::new(
            "oneclick_apply_payload_prohibited",
            "One-Click apply payload is prohibited.",
        )
    };
    let root = request.payload.as_object().ok_or_else(prohibited)?;
    if !object_has_only_keys(root, ROOT_KEYS) {
        return Err(prohibited());
    }
    if root.get("dry_run").and_then(Value::as_bool) != Some(false) {
        return Err(prohibited());
    }
    if root.get("backup_confirmed").and_then(Value::as_bool) != Some(true) {
        return Err(OneClickContractError::new(
            "oneclick_backup_required",
            "One-Click apply requires confirmed backup evidence.",
        ));
    }

    let connection = root
        .get("connection")
        .and_then(Value::as_object)
        .ok_or_else(prohibited)?;
    if !object_has_only_keys(connection, ENDPOINT_KEYS) {
        return Err(prohibited());
    }
    let schema = root
        .get("schema")
        .and_then(Value::as_str)
        .ok_or_else(prohibited)
        .and_then(normalize_oneclick_schema)?;
    for key in ["database", "schema"] {
        if connection
            .get(key)
            .is_some_and(|value| value.as_str() != Some(schema.as_str()))
        {
            return Err(OneClickContractError::new(
                "oneclick_schema_mismatch",
                "One-Click schema does not match the connection schema.",
            ));
        }
    }
    let endpoint_wire: OneClickEndpointWire =
        serde_json::from_value(Value::Object(connection.clone())).map_err(|_| prohibited())?;
    if endpoint_wire.engine != "mysql"
        || endpoint_wire.host.trim().is_empty()
        || endpoint_wire.user.trim().is_empty()
        || endpoint_wire
            .database
            .as_deref()
            .is_some_and(|nested| nested != schema)
        || endpoint_wire
            .schema
            .as_deref()
            .is_some_and(|nested| nested != schema)
    {
        return Err(prohibited());
    }

    let approval = root
        .get("approval")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            OneClickContractError::new(
                "oneclick_approval_required",
                "One-Click apply requires an approval artifact.",
            )
        })?;
    if !object_has_only_keys(approval, APPROVAL_KEYS) {
        return Err(prohibited());
    }
    if approval
        .get("approval_version")
        .and_then(Value::as_u64)
        != Some(u64::from(ONECLICK_APPROVAL_VERSION))
        || approval.get("plan_version").and_then(Value::as_u64)
            != Some(u64::from(ONECLICK_PLAN_VERSION))
    {
        return Err(OneClickContractError::new(
            "oneclick_approval_version_unsupported",
            "The One-Click approval version is unsupported.",
        ));
    }

    let profile_value = approval.get("remediation_profile").ok_or_else(|| {
        OneClickContractError::new(
            "oneclick_profile_required",
            "One-Click apply requires a remediation profile.",
        )
    })?;
    let profile_object = profile_value.as_object().ok_or_else(|| {
        OneClickContractError::new(
            "oneclick_profile_required",
            "One-Click apply requires a remediation profile.",
        )
    })?;
    if !object_has_only_keys(profile_object, PROFILE_KEYS) {
        return Err(prohibited());
    }
    if profile_object
        .get("profile_version")
        .and_then(Value::as_u64)
        != Some(u64::from(ONECLICK_PROFILE_VERSION))
    {
        return Err(OneClickContractError::new(
            "oneclick_profile_unsupported",
            "The One-Click remediation profile version is unsupported.",
        ));
    }
    let profile: OneClickRemediationProfile = serde_json::from_value(profile_value.clone())
        .map_err(|_| {
            OneClickContractError::new(
                "oneclick_profile_required",
                "One-Click apply requires a complete remediation profile.",
            )
        })?;
    if profile != fixed_oneclick_profile() {
        return Err(OneClickContractError::new(
            "oneclick_profile_substitution",
            "The approved One-Click remediation profile was substituted.",
        ));
    }

    let identity = approval
        .get("target_identity")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            OneClickContractError::new(
                "oneclick_approval_required",
                "One-Click apply requires a complete approval artifact.",
            )
        })?;
    let route = identity
        .get("route")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            OneClickContractError::new(
                "oneclick_approval_required",
                "One-Click apply requires a complete approval artifact.",
            )
        })?;
    if !object_has_only_keys(identity, IDENTITY_KEYS) || !object_has_only_keys(route, ROUTE_KEYS) {
        return Err(prohibited());
    }
    let approval: OneClickApprovalArtifact =
        serde_json::from_value(Value::Object(approval.clone())).map_err(|_| {
            OneClickContractError::new(
                "oneclick_approval_required",
                "One-Click apply requires a complete approval artifact.",
            )
        })?;
    if approval.target_identity.schema != schema {
        return Err(OneClickContractError::new(
            "oneclick_schema_mismatch",
            "One-Click approval schema does not match the request schema.",
        ));
    }
    if !has_text(&approval.target_identity.engine)
        || !has_text(&approval.target_identity.route.host)
        || !has_text(&approval.target_identity.server_uuid)
        || !has_text(&approval.target_identity.authenticated_user)
        || !is_sha256_hex(&approval.snapshot_hash)
        || !is_sha256_hex(&approval.plan_hash)
    {
        return Err(OneClickContractError::new(
            "oneclick_approval_required",
            "One-Click apply requires a complete approval artifact.",
        ));
    }

    Ok(ValidatedOneClickApplyRequest {
        endpoint: Endpoint {
            engine: endpoint_wire.engine,
            host: endpoint_wire.host,
            port: endpoint_wire.port,
            user: endpoint_wire.user,
            password: endpoint_wire.password,
            database: schema.clone(),
            schema: Some(schema.clone()),
        },
        schema,
        approval,
    })
}

fn has_text(value: &str) -> bool {
    !value.trim().is_empty() && !value.contains('\0')
}

fn invalid_facts() -> OneClickContractError {
    OneClickContractError::new(
        "oneclick_plan_invalid_facts",
        "One-Click planning facts are invalid.",
    )
}

fn noncanonical_facts() -> OneClickContractError {
    OneClickContractError::new(
        "oneclick_plan_noncanonical",
        "One-Click planning facts are not canonical.",
    )
}

fn normalize_tables_and_foreign_keys(
    tables: &mut Vec<ActionTableDefinitionFact>,
    foreign_keys: &mut Vec<ActionForeignKeyFact>,
) -> Result<(), OneClickContractError> {
    tables.sort_by(|left, right| (&left.schema, &left.table).cmp(&(&right.schema, &right.table)));
    let mut table_keys = BTreeSet::new();
    let mut table_columns = BTreeMap::<(String, String), BTreeSet<String>>::new();
    for table in tables {
        if !has_text(&table.schema)
            || !has_text(&table.table)
            || !table_keys.insert((table.schema.clone(), table.table.clone()))
        {
            return Err(invalid_facts());
        }
        if table
            .engine
            .as_deref()
            .is_some_and(|value| !has_text(value))
            || table
                .charset
                .as_deref()
                .is_some_and(|value| !has_text(value))
            || table
                .collation
                .as_deref()
                .is_some_and(|value| !has_text(value))
        {
            return Err(invalid_facts());
        }
        table.columns.sort_by_key(|column| column.ordinal_position);
        let mut column_names = BTreeSet::new();
        for (index, column) in table.columns.iter().enumerate() {
            if column.ordinal_position != (index + 1) as u32 {
                return Err(noncanonical_facts());
            }
            if !has_text(&column.name)
                || !has_text(&column.column_type)
                || !column_names.insert(column.name.clone())
            {
                return Err(invalid_facts());
            }
            match (
                column.generated_expression.as_deref(),
                column.generated_stored,
            ) {
                (None, None) => {}
                (Some(expression), Some(_)) if has_text(expression) => {}
                _ => return Err(invalid_facts()),
            }
            if column
                .charset
                .as_deref()
                .is_some_and(|value| !has_text(value))
                || column
                    .collation
                    .as_deref()
                    .is_some_and(|value| !has_text(value))
            {
                return Err(invalid_facts());
            }
        }
        table.indexes.sort_by(|left, right| left.name.cmp(&right.name));
        let mut index_names = BTreeSet::new();
        for index in &mut table.indexes {
            if !has_text(&index.name)
                || !has_text(&index.index_type)
                || index.columns.is_empty()
                || !index_names.insert(index.name.clone())
            {
                return Err(invalid_facts());
            }
            index
                .columns
                .sort_by_key(|column| column.ordinal_position);
            for (position, column) in index.columns.iter().enumerate() {
                if column.ordinal_position != (position + 1) as u32 {
                    return Err(noncanonical_facts());
                }
                let valid_column = column.column_name.as_deref().is_some_and(has_text);
                let valid_expression = column.expression.as_deref().is_some_and(has_text);
                if valid_column == valid_expression
                    || column.prefix_length == Some(0)
                    || column
                        .column_name
                        .as_ref()
                        .is_some_and(|name| !column_names.contains(name))
                {
                    return Err(invalid_facts());
                }
            }
        }
        table_columns.insert(
            (table.schema.clone(), table.table.clone()),
            column_names,
        );
    }

    foreign_keys.sort_by(|left, right| {
        (
            &left.constraint_schema,
            &left.constraint_name,
            &left.table_schema,
            &left.table_name,
            &left.referenced_table_schema,
            &left.referenced_table_name,
        )
            .cmp(&(
                &right.constraint_schema,
                &right.constraint_name,
                &right.table_schema,
                &right.table_name,
                &right.referenced_table_schema,
                &right.referenced_table_name,
            ))
    });
    let mut fk_keys = BTreeSet::new();
    for foreign_key in foreign_keys {
        if [
            &foreign_key.constraint_schema,
            &foreign_key.constraint_name,
            &foreign_key.table_schema,
            &foreign_key.table_name,
            &foreign_key.referenced_table_schema,
            &foreign_key.referenced_table_name,
            &foreign_key.match_option,
            &foreign_key.update_rule,
            &foreign_key.delete_rule,
        ]
        .into_iter()
        .any(|value| !has_text(value))
            || foreign_key.constraint_schema != foreign_key.table_schema
            || foreign_key.table_schema != foreign_key.referenced_table_schema
            || foreign_key.columns.is_empty()
            || !fk_keys.insert((
                foreign_key.constraint_schema.clone(),
                foreign_key.constraint_name.clone(),
                foreign_key.table_schema.clone(),
                foreign_key.table_name.clone(),
                foreign_key.referenced_table_schema.clone(),
                foreign_key.referenced_table_name.clone(),
            ))
        {
            return Err(invalid_facts());
        }
        foreign_key
            .columns
            .sort_by_key(|column| column.ordinal_position);
        for (position, column) in foreign_key.columns.iter().enumerate() {
            if column.ordinal_position != (position + 1) as u32 {
                return Err(noncanonical_facts());
            }
            if !has_text(&column.column_name) || !has_text(&column.referenced_column_name) {
                return Err(invalid_facts());
            }
            let table_key = (
                foreign_key.table_schema.clone(),
                foreign_key.table_name.clone(),
            );
            let referenced_table_key = (
                foreign_key.referenced_table_schema.clone(),
                foreign_key.referenced_table_name.clone(),
            );
            if !table_columns
                .get(&table_key)
                .is_some_and(|columns| columns.contains(&column.column_name))
                || !table_columns
                    .get(&referenced_table_key)
                    .is_some_and(|columns| columns.contains(&column.referenced_column_name))
            {
                return Err(invalid_facts());
            }
        }
    }
    Ok(())
}

pub(crate) fn normalize_action_facts(
    mut facts: ActionFactsDocument,
) -> Result<ActionFactsDocument, OneClickContractError> {
    if facts.action_facts_version != ACTION_FACTS_VERSION || facts.tables.is_empty() {
        return Err(invalid_facts());
    }
    normalize_tables_and_foreign_keys(&mut facts.tables, &mut facts.foreign_keys)?;
    Ok(facts)
}

fn validate_canonical_action_facts(
    facts: &ActionFactsDocument,
) -> Result<(), OneClickContractError> {
    if normalize_action_facts(facts.clone())? != *facts {
        return Err(noncanonical_facts());
    }
    Ok(())
}

#[allow(dead_code)]
pub(crate) fn canonical_action_facts_json(
    facts: &ActionFactsDocument,
) -> Result<String, OneClickContractError> {
    validate_canonical_action_facts(facts)?;
    serde_json::to_string(facts).map_err(|_| invalid_facts())
}

fn domain_hash<T: Serialize>(domain: &[u8], value: &T) -> Result<String, OneClickContractError> {
    let bytes = serde_json::to_vec(value).map_err(|_| invalid_facts())?;
    let mut hasher = Sha256::new();
    hasher.update(domain);
    hasher.update(bytes);
    Ok(hex::encode(hasher.finalize()))
}

pub(crate) fn hash_action_facts(
    facts: &ActionFactsDocument,
) -> Result<String, OneClickContractError> {
    validate_canonical_action_facts(facts)?;
    domain_hash(ACTION_FACTS_HASH_DOMAIN, facts)
}

fn normalize_snapshot(
    mut snapshot: OneClickSnapshotDocument,
) -> Result<OneClickSnapshotDocument, OneClickContractError> {
    if snapshot.snapshot_version != ONECLICK_SNAPSHOT_VERSION || !has_text(&snapshot.schema) {
        return Err(invalid_facts());
    }
    snapshot.inspection_facts.sort_by(|left, right| {
        (
            &left.issue_type,
            &left.severity,
            &left.object_kind,
            &left.schema,
            left.table.as_deref().unwrap_or(""),
            left.column.as_deref().unwrap_or(""),
        )
            .cmp(&(
                &right.issue_type,
                &right.severity,
                &right.object_kind,
                &right.schema,
                right.table.as_deref().unwrap_or(""),
                right.column.as_deref().unwrap_or(""),
            ))
    });
    for fact in &snapshot.inspection_facts {
        if !has_text(&fact.issue_type)
            || !has_text(&fact.severity)
            || !has_text(&fact.object_kind)
            || !has_text(&fact.schema)
            || fact.table.as_deref().is_some_and(|value| !has_text(value))
            || fact.column.as_deref().is_some_and(|value| !has_text(value))
        {
            return Err(invalid_facts());
        }
    }
    normalize_tables_and_foreign_keys(
        &mut snapshot.table_definitions,
        &mut snapshot.foreign_keys,
    )?;
    if snapshot
        .inspection_facts
        .iter()
        .any(|fact| fact.schema != snapshot.schema)
        || snapshot
            .table_definitions
            .iter()
            .any(|table| table.schema != snapshot.schema)
        || snapshot.foreign_keys.iter().any(|foreign_key| {
            foreign_key.constraint_schema != snapshot.schema
                || foreign_key.table_schema != snapshot.schema
                || foreign_key.referenced_table_schema != snapshot.schema
        })
    {
        return Err(invalid_facts());
    }
    let table_keys = snapshot
        .table_definitions
        .iter()
        .map(|table| (table.schema.as_str(), table.table.as_str()))
        .collect::<BTreeSet<_>>();
    let mut inspection_keys = BTreeSet::new();
    for fact in &snapshot.inspection_facts {
        let Some(table) = fact.table.as_deref() else {
            return Err(invalid_facts());
        };
        if !table_keys.contains(&(fact.schema.as_str(), table))
            || !inspection_keys.insert((
                fact.issue_type.as_str(),
                fact.severity.as_str(),
                fact.object_kind.as_str(),
                fact.schema.as_str(),
                table,
                fact.column.as_deref(),
            ))
        {
            return Err(invalid_facts());
        }
    }
    Ok(snapshot)
}

fn snapshot_deprecated_engine_tables(
    snapshot: &OneClickSnapshotDocument,
    profile: &OneClickRemediationProfile,
) -> Result<BTreeSet<String>, OneClickContractError> {
    let tables = snapshot
        .table_definitions
        .iter()
        .map(|table| (table.table.as_str(), table))
        .collect::<BTreeMap<_, _>>();
    let expected_charset_tables = snapshot
        .table_definitions
        .iter()
        .filter(|table| {
            table.charset.as_deref() != Some(profile.target_charset.as_str())
                || table.collation.as_deref() != Some(profile.target_collation.as_str())
        })
        .map(|table| table.table.clone())
        .collect::<BTreeSet<_>>();
    let mut charset_tables = BTreeSet::new();
    let mut deprecated_engine_tables = BTreeSet::new();
    for fact in &snapshot.inspection_facts {
        let table_name = fact.table.as_deref().ok_or_else(invalid_facts)?;
        let table = tables.get(table_name).ok_or_else(invalid_facts)?;
        if fact.severity != "warning"
            || fact.object_kind != "table"
            || fact.schema != snapshot.schema
            || fact.column.is_some()
        {
            return Err(invalid_facts());
        }
        match fact.issue_type.as_str() {
            "deprecated_engine"
                if table
                    .engine
                    .as_deref()
                    .is_some_and(|engine| !engine.eq_ignore_ascii_case("InnoDB")) =>
            {
                deprecated_engine_tables.insert(table_name.to_string());
            }
            "charset_issue" if expected_charset_tables.contains(table_name) => {
                charset_tables.insert(table_name.to_string());
            }
            _ => return Err(invalid_facts()),
        }
    }
    if charset_tables != expected_charset_tables {
        return Err(invalid_facts());
    }
    Ok(deprecated_engine_tables)
}

fn hash_snapshot(snapshot: &OneClickSnapshotDocument) -> Result<String, OneClickContractError> {
    domain_hash(SNAPSHOT_HASH_DOMAIN, snapshot)
}

pub(crate) fn compute_oneclick_plan_hash(
    plan: &OneClickPlanEnvelope,
) -> Result<String, OneClickContractError> {
    domain_hash(
        PLAN_HASH_DOMAIN,
        &OneClickPlanHashDocument {
            plan_version: plan.plan_version,
            target_identity: &plan.target_identity,
            remediation_profile: &plan.remediation_profile,
            snapshot_hash: &plan.snapshot_hash,
            actions: &plan.actions,
        },
    )
}

fn action_expectation(
    facts: ActionFactsDocument,
) -> Result<OneClickActionStateExpectation, OneClickContractError> {
    let facts = normalize_action_facts(facts)?;
    let facts_hash = hash_action_facts(&facts)?;
    Ok(OneClickActionStateExpectation { facts, facts_hash })
}

fn sql_is_one_statement(sql: &str) -> bool {
    let bytes = sql.trim().as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let mut quote = None;
    let mut terminal_separator = None;
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if let Some(delimiter) = quote {
            if byte == b'\\' {
                index += 2;
                continue;
            }
            if byte == delimiter {
                if bytes.get(index + 1) == Some(&delimiter) {
                    index += 2;
                    continue;
                }
                quote = None;
            }
        } else {
            match byte {
                b'`' | b'\'' | b'"' => quote = Some(byte),
                b';' => {
                    if terminal_separator.is_some() {
                        return false;
                    }
                    terminal_separator = Some(index);
                }
                _ if terminal_separator.is_some() && !byte.is_ascii_whitespace() => {
                    return false;
                }
                _ => {}
            }
        }
        index += 1;
    }
    quote.is_none() && terminal_separator == Some(bytes.len() - 1)
}

fn action_facts_within_tables(action: &OneClickApplyAction, facts: &ActionFactsDocument) -> bool {
    let allowed = action.tables.iter().map(String::as_str).collect::<BTreeSet<_>>();
    facts
        .tables
        .iter()
        .all(|table| table.schema == action.schema && allowed.contains(table.table.as_str()))
        && facts.foreign_keys.iter().all(|foreign_key| {
            foreign_key.constraint_schema == action.schema
                && foreign_key.table_schema == action.schema
                && foreign_key.referenced_table_schema == action.schema
                && allowed.contains(foreign_key.table_name.as_str())
                && allowed.contains(foreign_key.referenced_table_name.as_str())
        })
}

pub(crate) fn validate_oneclick_plan(
    plan: &OneClickPlanEnvelope,
) -> Result<(), OneClickContractError> {
    if plan.plan_version != ONECLICK_PLAN_VERSION
        || plan.remediation_profile != fixed_oneclick_profile()
        || plan.snapshot != normalize_snapshot(plan.snapshot.clone())?
        || plan.snapshot_hash != hash_snapshot(&plan.snapshot)?
        || plan.target_identity.schema != plan.snapshot.schema
    {
        return Err(noncanonical_facts());
    }
    let deprecated_engine_tables =
        snapshot_deprecated_engine_tables(&plan.snapshot, &plan.remediation_profile)?;
    for (position, action) in plan.actions.iter().enumerate() {
        if action.ordinal != (position + 1) as u32
            || action.schema != plan.snapshot.schema
            || action.tables.is_empty()
            || action.tables.iter().any(|table| !has_text(table))
            || !sql_is_one_statement(&action.sql)
            || action
                .rollback_sql
                .as_deref()
                .is_some_and(|sql| !sql_is_one_statement(sql))
            || action.expected_pre_facts.facts.action_type != action.action_type
            || action.expected_post_facts.facts.action_type != action.action_type
        {
            return Err(invalid_facts());
        }
        if action
            .tables
            .windows(2)
            .any(|pair| pair[0].as_bytes() >= pair[1].as_bytes())
        {
            return Err(noncanonical_facts());
        }
        validate_canonical_action_facts(&action.expected_pre_facts.facts)?;
        validate_canonical_action_facts(&action.expected_post_facts.facts)?;
        if action.expected_pre_facts.facts_hash
            != hash_action_facts(&action.expected_pre_facts.facts)?
            || action.expected_post_facts.facts_hash
                != hash_action_facts(&action.expected_post_facts.facts)?
            || !action_facts_within_tables(action, &action.expected_pre_facts.facts)
            || !action_facts_within_tables(action, &action.expected_post_facts.facts)
        {
            return Err(noncanonical_facts());
        }
    }
    let expected_actions = build_oneclick_actions(
        &plan.snapshot.schema,
        &plan.snapshot.table_definitions,
        &plan.snapshot.foreign_keys,
        &plan.remediation_profile,
        &deprecated_engine_tables,
    )?;
    if plan.actions != expected_actions {
        return Err(noncanonical_facts());
    }
    if plan.plan_hash != compute_oneclick_plan_hash(plan)? {
        return Err(noncanonical_facts());
    }
    Ok(())
}

pub(crate) fn oneclick_approval_artifact(plan: &OneClickPlanEnvelope) -> OneClickApprovalArtifact {
    OneClickApprovalArtifact {
        approval_version: ONECLICK_APPROVAL_VERSION,
        plan_version: plan.plan_version,
        target_identity: plan.target_identity.clone(),
        remediation_profile: plan.remediation_profile.clone(),
        snapshot_hash: plan.snapshot_hash.clone(),
        plan_hash: plan.plan_hash.clone(),
    }
}

pub(crate) trait OneClickPlanningSession {
    fn profile_supported(
        &mut self,
        profile: &OneClickRemediationProfile,
    ) -> Result<bool, String>;
    fn read_target_identity(
        &mut self,
        endpoint: &Endpoint,
    ) -> Result<OneClickTargetIdentity, String>;
    fn inspect(&mut self, endpoint: &Endpoint) -> Result<InspectionResult, String>;
    fn read_table_definitions(
        &mut self,
        schema: &str,
    ) -> Result<Vec<ActionTableDefinitionFact>, String>;
    fn read_fk_facts(&mut self, schema: &str) -> Result<Vec<ActionForeignKeyFact>, String>;
}

pub(crate) trait OneClickApplySession: OneClickPlanningSession {
    fn acquire_advisory_lock(&mut self, key: &str, seconds: u32) -> Result<bool, String>;
    fn release_advisory_lock(&mut self, key: &str) -> Result<(), String>;
    fn read_action_facts(
        &mut self,
        action: &OneClickApplyAction,
    ) -> Result<ActionFactsDocument, String>;
    fn execute_sql(&mut self, sql: &str) -> Result<(), String>;
}

fn planning_failed() -> OneClickContractError {
    OneClickContractError::new(
        "oneclick_plan_failed",
        "One-Click plan could not be built from the target.",
    )
}

fn quote_mysql_identifier(identifier: &str) -> String {
    format!("`{}`", identifier.replace('`', "``"))
}

fn rollback_token(value: &str) -> Option<&str> {
    if !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
    {
        Some(value)
    } else {
        None
    }
}

fn scoped_action_facts(
    action_type: OneClickActionType,
    tables: &[ActionTableDefinitionFact],
    foreign_keys: &[ActionForeignKeyFact],
    names: &BTreeSet<String>,
) -> Result<ActionFactsDocument, OneClickContractError> {
    normalize_action_facts(ActionFactsDocument {
        action_facts_version: ACTION_FACTS_VERSION,
        action_type,
        tables: tables
            .iter()
            .filter(|table| names.contains(&table.table))
            .cloned()
            .collect(),
        foreign_keys: foreign_keys
            .iter()
            .filter(|foreign_key| {
                names.contains(&foreign_key.table_name)
                    && names.contains(&foreign_key.referenced_table_name)
            })
            .cloned()
            .collect(),
    })
}

fn update_charset_fact(
    tables: &mut [ActionTableDefinitionFact],
    table_name: &str,
    profile: &OneClickRemediationProfile,
) {
    if let Some(table) = tables.iter_mut().find(|table| table.table == table_name) {
        table.charset = Some(profile.target_charset.clone());
        table.collation = Some(profile.target_collation.clone());
        for column in &mut table.columns {
            if column.charset.is_some() {
                column.charset = Some(profile.target_charset.clone());
                column.collation = Some(profile.target_collation.clone());
            }
        }
    }
}

fn validated_deprecated_engine_tables(
    unsupported_objects: &[String],
    tables: &[ActionTableDefinitionFact],
) -> Result<BTreeSet<String>, OneClickContractError> {
    let mut markers = BTreeMap::<String, String>::new();
    for object in unsupported_objects {
        if !object.starts_with("deprecated_engine:") {
            continue;
        }
        let (table, engine) =
            oneclick_deprecated_engine_marker(object).ok_or_else(invalid_facts)?;
        if markers
            .get(&table)
            .is_some_and(|existing| !existing.eq_ignore_ascii_case(&engine))
        {
            return Err(invalid_facts());
        }
        markers.entry(table).or_insert(engine);
    }

    let definitions = tables
        .iter()
        .map(|table| (table.table.as_str(), table))
        .collect::<BTreeMap<_, _>>();
    for (table_name, marker_engine) in &markers {
        let table = definitions
            .get(table_name.as_str())
            .ok_or_else(invalid_facts)?;
        if !table
            .engine
            .as_deref()
            .is_some_and(|engine| engine.eq_ignore_ascii_case(marker_engine))
        {
            return Err(invalid_facts());
        }
    }
    Ok(markers.into_keys().collect())
}

fn build_oneclick_actions(
    schema: &str,
    tables: &[ActionTableDefinitionFact],
    foreign_keys: &[ActionForeignKeyFact],
    profile: &OneClickRemediationProfile,
    deprecated_engine_tables: &BTreeSet<String>,
) -> Result<Vec<OneClickApplyAction>, OneClickContractError> {
    let mut working = tables.to_vec();
    let mut actions = Vec::new();

    for table_name in tables
        .iter()
        .filter(|table| deprecated_engine_tables.contains(&table.table))
        .map(|table| table.table.clone())
    {
        let names = BTreeSet::from([table_name.clone()]);
        let pre = scoped_action_facts(
            OneClickActionType::EngineInnodb,
            &working,
            &[],
            &names,
        )?;
        let previous_engine = pre.tables[0].engine.clone();
        if let Some(table) = working.iter_mut().find(|table| table.table == table_name) {
            table.engine = Some("InnoDB".to_string());
        }
        let post = scoped_action_facts(
            OneClickActionType::EngineInnodb,
            &working,
            &[],
            &names,
        )?;
        let rollback_sql = previous_engine
            .as_deref()
            .and_then(rollback_token)
            .map(|engine| {
                format!(
                    "ALTER TABLE {}.{} ENGINE={engine};",
                    quote_mysql_identifier(schema),
                    quote_mysql_identifier(&table_name)
                )
            });
        actions.push(OneClickApplyAction {
            ordinal: (actions.len() + 1) as u32,
            action_type: OneClickActionType::EngineInnodb,
            issue_type: "deprecated_engine".to_string(),
            strategy: "engine_innodb".to_string(),
            schema: schema.to_string(),
            tables: vec![table_name.clone()],
            sql: format!(
                "ALTER TABLE {}.{} ENGINE=InnoDB;",
                quote_mysql_identifier(schema),
                quote_mysql_identifier(&table_name)
            ),
            rollback_sql,
            target_charset: None,
            target_collation: None,
            expected_pre_facts: action_expectation(pre)?,
            expected_post_facts: action_expectation(post)?,
        });
    }

    let fk_tables = foreign_keys
        .iter()
        .flat_map(|foreign_key| {
            [
                foreign_key.table_name.as_str(),
                foreign_key.referenced_table_name.as_str(),
            ]
        })
        .collect::<BTreeSet<_>>();
    let charset_tables = working
        .iter()
        .filter(|table| {
            !fk_tables.contains(table.table.as_str())
                && (table.charset.as_deref() != Some(profile.target_charset.as_str())
                    || table.collation.as_deref()
                        != Some(profile.target_collation.as_str()))
        })
        .map(|table| table.table.clone())
        .collect::<Vec<_>>();
    for table_name in charset_tables {
        let names = BTreeSet::from([table_name.clone()]);
        let pre = scoped_action_facts(
            OneClickActionType::CharsetFkSafe,
            &working,
            foreign_keys,
            &names,
        )?;
        let previous = working
            .iter()
            .find(|table| table.table == table_name)
            .and_then(|table| Some((table.charset.clone()?, table.collation.clone()?)));
        update_charset_fact(&mut working, &table_name, profile);
        let post = scoped_action_facts(
            OneClickActionType::CharsetFkSafe,
            &working,
            foreign_keys,
            &names,
        )?;
        let rollback_sql = previous.and_then(|(charset, collation)| {
            Some(format!(
                "ALTER TABLE {}.{} CONVERT TO CHARACTER SET {} COLLATE {};",
                quote_mysql_identifier(schema),
                quote_mysql_identifier(&table_name),
                rollback_token(&charset)?,
                rollback_token(&collation)?
            ))
        });
        actions.push(OneClickApplyAction {
            ordinal: (actions.len() + 1) as u32,
            action_type: OneClickActionType::CharsetFkSafe,
            issue_type: "charset_issue".to_string(),
            strategy: "charset_fk_safe".to_string(),
            schema: schema.to_string(),
            tables: vec![table_name.clone()],
            sql: format!(
                "ALTER TABLE {}.{} CONVERT TO CHARACTER SET {} COLLATE {};",
                quote_mysql_identifier(schema),
                quote_mysql_identifier(&table_name),
                profile.target_charset,
                profile.target_collation
            ),
            rollback_sql,
            target_charset: Some(profile.target_charset.clone()),
            target_collation: Some(profile.target_collation.clone()),
            expected_pre_facts: action_expectation(pre)?,
            expected_post_facts: action_expectation(post)?,
        });
    }
    Ok(actions)
}

pub(crate) fn build_oneclick_plan<S: OneClickPlanningSession>(
    session: &mut S,
    endpoint: &Endpoint,
    schema: &str,
) -> Result<OneClickPlanEnvelope, OneClickContractError> {
    let schema = normalize_oneclick_schema(schema)?;
    let profile = fixed_oneclick_profile();
    if !session
        .profile_supported(&profile)
        .map_err(|_| planning_failed())?
    {
        return Err(OneClickContractError::new(
            "oneclick_profile_unsupported",
            "The fixed One-Click remediation profile is unsupported by the target.",
        ));
    }
    let identity = session
        .read_target_identity(endpoint)
        .map_err(|_| planning_failed())?;
    if identity.engine != endpoint.engine
        || identity.route.host != endpoint.host
        || identity.route.port != endpoint.port
        || identity.schema != schema
        || !has_text(&identity.server_uuid)
        || !has_text(&identity.authenticated_user)
    {
        return Err(OneClickContractError::new(
            "oneclick_target_changed",
            "The One-Click target identity changed.",
        ));
    }
    let inspection = session.inspect(endpoint).map_err(|_| planning_failed())?;
    let tables = session
        .read_table_definitions(&schema)
        .map_err(|_| planning_failed())?;
    let foreign_keys = session
        .read_fk_facts(&schema)
        .map_err(|_| planning_failed())?;
    let mut snapshot = normalize_snapshot(OneClickSnapshotDocument {
        snapshot_version: ONECLICK_SNAPSHOT_VERSION,
        schema: schema.clone(),
        inspection_facts: Vec::new(),
        table_definitions: tables,
        foreign_keys,
    })?;
    let deprecated_engine_tables = validated_deprecated_engine_tables(
        &inspection.unsupported_objects,
        &snapshot.table_definitions,
    )?;
    let mut inspection_facts = Vec::new();
    for table in &snapshot.table_definitions {
        if deprecated_engine_tables.contains(&table.table) {
            inspection_facts.push(OneClickInspectionFact {
                issue_type: "deprecated_engine".to_string(),
                severity: "warning".to_string(),
                object_kind: "table".to_string(),
                schema: schema.clone(),
                table: Some(table.table.clone()),
                column: None,
            });
        }
        if table.charset.as_deref() != Some(profile.target_charset.as_str())
            || table.collation.as_deref() != Some(profile.target_collation.as_str())
        {
            inspection_facts.push(OneClickInspectionFact {
                issue_type: "charset_issue".to_string(),
                severity: "warning".to_string(),
                object_kind: "table".to_string(),
                schema: schema.clone(),
                table: Some(table.table.clone()),
                column: None,
            });
        }
    }
    snapshot.inspection_facts = inspection_facts;
    snapshot = normalize_snapshot(snapshot)?;
    if snapshot_deprecated_engine_tables(&snapshot, &profile)? != deprecated_engine_tables {
        return Err(invalid_facts());
    }
    let snapshot_hash = hash_snapshot(&snapshot)?;
    let actions = build_oneclick_actions(
        &schema,
        &snapshot.table_definitions,
        &snapshot.foreign_keys,
        &profile,
        &deprecated_engine_tables,
    )?;
    let mut plan = OneClickPlanEnvelope {
        plan_version: ONECLICK_PLAN_VERSION,
        target_identity: identity,
        remediation_profile: profile,
        snapshot,
        snapshot_hash,
        actions,
        plan_hash: String::new(),
    };
    plan.plan_hash = compute_oneclick_plan_hash(&plan)?;
    validate_oneclick_plan(&plan)?;
    Ok(plan)
}

fn normalize_oneclick_server_uuid(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    if bytes.len() != 32 && bytes.len() != 36 {
        return None;
    }
    if bytes.len() == 36
        && [8usize, 13, 18, 23]
            .into_iter()
            .any(|position| bytes[position] != b'-')
    {
        return None;
    }
    let compact = bytes
        .iter()
        .enumerate()
        .filter_map(|(position, byte)| {
            if bytes.len() == 36 && [8usize, 13, 18, 23].contains(&position) {
                None
            } else {
                Some(byte.to_ascii_lowercase())
            }
        })
        .collect::<Vec<_>>();
    if compact.len() != 32 || !compact.iter().all(u8::is_ascii_hexdigit) {
        return None;
    }
    let compact = String::from_utf8(compact).ok()?;
    Some(format!(
        "{}-{}-{}-{}-{}",
        &compact[0..8],
        &compact[8..12],
        &compact[12..16],
        &compact[16..20],
        &compact[20..32]
    ))
}

pub(crate) fn oneclick_advisory_lock_key(
    identity: &OneClickTargetIdentity,
) -> Result<String, OneClickContractError> {
    let server_uuid = normalize_oneclick_server_uuid(&identity.server_uuid).ok_or_else(|| {
        OneClickContractError::new(
            "oneclick_target_changed",
            "The One-Click target server UUID is invalid.",
        )
    })?;
    let mut digest = Sha256::new();
    digest.update(LOCK_KEY_HASH_DOMAIN);
    digest.update(server_uuid.as_bytes());
    let key = format!("tf1:{}", URL_SAFE_NO_PAD.encode(digest.finalize()));
    debug_assert_eq!(key.len(), 47);
    Ok(key)
}

fn oneclick_replan_error(error: OneClickContractError) -> OneClickContractError {
    match error.code() {
        "oneclick_profile_unsupported" | "oneclick_target_changed" => error,
        _ => OneClickContractError::new(
            "oneclick_replan_failed",
            "The approved One-Click plan could not be rebuilt.",
        ),
    }
}

fn execute_locked_oneclick<S: OneClickApplySession>(
    session: &mut S,
    validated: &ValidatedOneClickApplyRequest,
) -> Result<OneClickApplyOutcome, OneClickContractError> {
    let replan = build_oneclick_plan(session, &validated.endpoint, &validated.schema)
        .map_err(oneclick_replan_error)?;
    if replan.target_identity != validated.approval.target_identity {
        return Err(OneClickContractError::new(
            "oneclick_target_changed",
            "The approved One-Click target identity changed.",
        ));
    }
    if replan.remediation_profile != validated.approval.remediation_profile {
        return Err(OneClickContractError::new(
            "oneclick_profile_substitution",
            "The approved One-Click remediation profile was substituted.",
        ));
    }
    if replan.actions.is_empty() {
        return Err(OneClickContractError::new(
            "oneclick_nothing_to_apply",
            "The approved One-Click plan no longer has actions to apply.",
        ));
    }
    if replan.snapshot_hash != validated.approval.snapshot_hash {
        return Err(OneClickContractError::new(
            "oneclick_snapshot_changed",
            "The approved One-Click target snapshot changed.",
        ));
    }
    if replan.plan_hash != validated.approval.plan_hash {
        return Err(OneClickContractError::new(
            "oneclick_plan_changed",
            "The approved One-Click plan changed.",
        ));
    }

    let mut applied_ordinals = Vec::new();
    for action in &replan.actions {
        let pre_facts = session.read_action_facts(action).map_err(|_| {
            OneClickContractError::new(
                "oneclick_precondition_changed",
                "A One-Click action precondition could not be verified.",
            )
            .with_applied_ordinals(applied_ordinals.clone())
        })?;
        if pre_facts != action.expected_pre_facts.facts {
            return Err(OneClickContractError::new(
                "oneclick_precondition_changed",
                "A One-Click action precondition changed.",
            )
            .with_applied_ordinals(applied_ordinals));
        }
        if session.execute_sql(&action.sql).is_err() {
            return Err(OneClickContractError::new(
                "oneclick_outcome_indeterminate",
                "A One-Click action may have committed before its result became unavailable.",
            )
            .with_applied_ordinals(applied_ordinals)
            .with_indeterminate_ordinal(action.ordinal));
        }
        applied_ordinals.push(action.ordinal);
        let post_facts = session.read_action_facts(action).map_err(|_| {
            OneClickContractError::new(
                "oneclick_postcondition_changed",
                "A One-Click action postcondition could not be verified.",
            )
            .with_applied_ordinals(applied_ordinals.clone())
        })?;
        if post_facts != action.expected_post_facts.facts {
            return Err(OneClickContractError::new(
                "oneclick_postcondition_changed",
                "A One-Click action postcondition changed.",
            )
            .with_applied_ordinals(applied_ordinals));
        }
    }
    Ok(OneClickApplyOutcome { applied_ordinals })
}

pub(crate) fn execute_approved_oneclick<S: OneClickApplySession>(
    session: &mut S,
    validated: &ValidatedOneClickApplyRequest,
) -> Result<OneClickApplyOutcome, OneClickContractError> {
    let identity = session
        .read_target_identity(&validated.endpoint)
        .map_err(|_| {
            OneClickContractError::new(
                "oneclick_replan_failed",
                "The One-Click target identity could not be read.",
            )
        })?;
    let lock_key = oneclick_advisory_lock_key(&identity)?;
    let acquired = session
        .acquire_advisory_lock(&lock_key, ONECLICK_LOCK_TIMEOUT_SECONDS)
        .map_err(|_| {
            OneClickContractError::new(
                "oneclick_lock_unavailable",
                "The One-Click server advisory lock is unavailable.",
            )
        })?;
    if !acquired {
        return Err(OneClickContractError::new(
            "oneclick_lock_unavailable",
            "The One-Click server advisory lock is unavailable.",
        ));
    }

    let result = execute_locked_oneclick(session, validated);
    match session.release_advisory_lock(&lock_key) {
        Ok(()) => result,
        Err(_) => match result {
            Ok(outcome) => Err(OneClickContractError::new(
                "oneclick_lock_unavailable",
                "The One-Click server advisory lock could not be released safely.",
            )
            .with_applied_ordinals(outcome.applied_ordinals)
            .with_lock_release_failed()),
            Err(error) => Err(error.with_lock_release_failed()),
        },
    }
}

pub(crate) struct LiveOneClickSession {
    conn: mysql::PooledConn,
}

impl LiveOneClickSession {
    pub(crate) fn connect(endpoint: &Endpoint) -> Result<Self, OneClickContractError> {
        let pool = mysql::Pool::new(mysql_opts(endpoint)).map_err(|_| planning_failed())?;
        let conn = pool.get_conn().map_err(|_| planning_failed())?;
        Ok(Self { conn })
    }
}

impl OneClickPlanningSession for LiveOneClickSession {
    fn profile_supported(
        &mut self,
        profile: &OneClickRemediationProfile,
    ) -> Result<bool, String> {
        let count = self
            .conn
            .exec_first::<u64, _, _>(
                "SELECT COUNT(*) FROM information_schema.collations WHERE CHARACTER_SET_NAME = ? AND COLLATION_NAME = ?",
                (&profile.target_charset, &profile.target_collation),
            )
            .map_err(|err| format!("mysql profile validation failed: {err}"))?
            .unwrap_or(0);
        Ok(count == 1)
    }

    fn read_target_identity(
        &mut self,
        endpoint: &Endpoint,
    ) -> Result<OneClickTargetIdentity, String> {
        let (server_uuid, authenticated_user) = self
            .conn
            .query_first::<(String, String), _>("SELECT @@server_uuid, CURRENT_USER()")
            .map_err(|err| format!("mysql identity query failed: {err}"))?
            .ok_or_else(|| "mysql identity query returned no row".to_string())?;
        if !has_text(&server_uuid) || !has_text(&authenticated_user) {
            return Err("mysql identity query returned invalid values".to_string());
        }
        Ok(OneClickTargetIdentity {
            engine: endpoint.engine.clone(),
            route: OneClickRoute {
                host: endpoint.host.clone(),
                port: endpoint.port,
            },
            server_uuid,
            authenticated_user,
            schema: endpoint.database.clone(),
        })
    }

    fn inspect(&mut self, endpoint: &Endpoint) -> Result<InspectionResult, String> {
        inspect_mysql_with_conn(&mut self.conn, &endpoint.database)
    }

    fn read_table_definitions(
        &mut self,
        schema: &str,
    ) -> Result<Vec<ActionTableDefinitionFact>, String> {
        let rows = self
            .conn
            .exec_map(
                "SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.ENGINE, c.CHARACTER_SET_NAME, t.TABLE_COLLATION FROM information_schema.tables t LEFT JOIN information_schema.collations c ON c.COLLATION_NAME = t.TABLE_COLLATION WHERE t.TABLE_SCHEMA = ? AND t.TABLE_TYPE = 'BASE TABLE' ORDER BY BINARY t.TABLE_SCHEMA, BINARY t.TABLE_NAME",
                (schema,),
                |(schema, table, engine, charset, collation): (
                    String,
                    String,
                    Option<String>,
                    Option<String>,
                    Option<String>,
                )| (schema, table, engine, charset, collation),
            )
            .map_err(|err| format!("mysql One-Click table metadata failed: {err}"))?;
        let mut tables = Vec::with_capacity(rows.len());
        for (table_schema, table_name, engine, charset, collation) in rows {
            let columns = self
                .conn
                .exec_map(
                    "SELECT ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, CHARACTER_SET_NAME, COLLATION_NAME, GENERATION_EXPRESSION FROM information_schema.columns WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                    (&table_schema, &table_name),
                    |(
                        ordinal_position,
                        name,
                        column_type,
                        is_nullable,
                        default_value,
                        extra,
                        charset,
                        collation,
                        generated_expression,
                    ): (
                        u32,
                        String,
                        String,
                        String,
                        Option<String>,
                        String,
                        Option<String>,
                        Option<String>,
                        Option<String>,
                    )| {
                        let nullable = is_nullable == "YES";
                        let generated_expression = generated_expression
                            .filter(|expression| !expression.is_empty());
                        let generated_stored = generated_expression.as_ref().map(|_| {
                            extra
                                .split_ascii_whitespace()
                                .any(|token| token == "STORED")
                        });
                        let default = match default_value {
                            _ if generated_expression.is_some() => ColumnDefaultFact::Absent,
                            Some(value) if extra.contains("DEFAULT_GENERATED") => {
                                ColumnDefaultFact::Expression(value)
                            }
                            Some(value) => ColumnDefaultFact::Literal(value),
                            None if nullable => ColumnDefaultFact::Null,
                            None => ColumnDefaultFact::Absent,
                        };
                        ActionColumnFact {
                            ordinal_position,
                            name,
                            column_type,
                            nullable,
                            default,
                            charset,
                            collation,
                            generated_expression,
                            generated_stored,
                        }
                    },
                )
                .map_err(|err| format!("mysql One-Click column metadata failed: {err}"))?;
            let index_rows = self
                .conn
                .exec_map(
                    "SELECT INDEX_NAME, NON_UNIQUE, INDEX_TYPE, IS_VISIBLE, SEQ_IN_INDEX, COLUMN_NAME, EXPRESSION, SUB_PART FROM information_schema.statistics WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY BINARY INDEX_NAME, SEQ_IN_INDEX",
                    (&table_schema, &table_name),
                    |(
                        name,
                        non_unique,
                        index_type,
                        is_visible,
                        ordinal_position,
                        column_name,
                        expression,
                        prefix_length,
                    ): (
                        String,
                        u8,
                        String,
                        String,
                        u32,
                        Option<String>,
                        Option<String>,
                        Option<u32>,
                    )| {
                        (
                            name,
                            non_unique == 0,
                            index_type,
                            is_visible == "YES",
                            ActionIndexColumnFact {
                                ordinal_position,
                                column_name,
                                expression,
                                prefix_length,
                            },
                        )
                    },
                )
                .map_err(|err| format!("mysql One-Click index metadata failed: {err}"))?;
            let mut indexes = Vec::<ActionIndexFact>::new();
            for (name, unique, index_type, visible, column) in index_rows {
                if let Some(index) = indexes.iter_mut().find(|index| index.name == name) {
                    if index.unique != unique
                        || index.index_type != index_type
                        || index.visible != visible
                    {
                        return Err("mysql One-Click index metadata is inconsistent".to_string());
                    }
                    index.columns.push(column);
                } else {
                    indexes.push(ActionIndexFact {
                        name,
                        unique,
                        index_type,
                        visible,
                        columns: vec![column],
                    });
                }
            }
            tables.push(ActionTableDefinitionFact {
                schema: table_schema,
                table: table_name,
                engine,
                charset,
                collation,
                columns,
                indexes,
            });
        }
        Ok(tables)
    }

    fn read_fk_facts(&mut self, schema: &str) -> Result<Vec<ActionForeignKeyFact>, String> {
        let rows = self
            .conn
            .exec_map(
                "SELECT k.CONSTRAINT_SCHEMA, k.CONSTRAINT_NAME, k.TABLE_SCHEMA, k.TABLE_NAME, k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, r.MATCH_OPTION, r.UPDATE_RULE, r.DELETE_RULE, k.ORDINAL_POSITION, k.COLUMN_NAME, k.REFERENCED_COLUMN_NAME FROM information_schema.key_column_usage k JOIN information_schema.referential_constraints r ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA = ? AND k.REFERENCED_TABLE_NAME IS NOT NULL ORDER BY BINARY k.CONSTRAINT_SCHEMA, BINARY k.CONSTRAINT_NAME, BINARY k.TABLE_SCHEMA, BINARY k.TABLE_NAME, BINARY k.REFERENCED_TABLE_SCHEMA, BINARY k.REFERENCED_TABLE_NAME, k.ORDINAL_POSITION",
                (schema,),
                |(
                    constraint_schema,
                    constraint_name,
                    table_schema,
                    table_name,
                    referenced_table_schema,
                    referenced_table_name,
                    match_option,
                    update_rule,
                    delete_rule,
                    ordinal_position,
                    column_name,
                    referenced_column_name,
                ): (
                    String,
                    String,
                    String,
                    String,
                    String,
                    String,
                    String,
                    String,
                    String,
                    u32,
                    String,
                    String,
                )| {
                    (
                        constraint_schema,
                        constraint_name,
                        table_schema,
                        table_name,
                        referenced_table_schema,
                        referenced_table_name,
                        match_option,
                        update_rule,
                        delete_rule,
                        ActionForeignKeyColumnFact {
                            ordinal_position,
                            column_name,
                            referenced_column_name,
                        },
                    )
                },
            )
            .map_err(|err| format!("mysql One-Click FK metadata failed: {err}"))?;
        let mut foreign_keys = Vec::<ActionForeignKeyFact>::new();
        for (
            constraint_schema,
            constraint_name,
            table_schema,
            table_name,
            referenced_table_schema,
            referenced_table_name,
            match_option,
            update_rule,
            delete_rule,
            column,
        ) in rows
        {
            if let Some(foreign_key) = foreign_keys.iter_mut().find(|foreign_key| {
                foreign_key.constraint_schema == constraint_schema
                    && foreign_key.constraint_name == constraint_name
                    && foreign_key.table_schema == table_schema
                    && foreign_key.table_name == table_name
                    && foreign_key.referenced_table_schema == referenced_table_schema
                    && foreign_key.referenced_table_name == referenced_table_name
            }) {
                if foreign_key.match_option != match_option
                    || foreign_key.update_rule != update_rule
                    || foreign_key.delete_rule != delete_rule
                {
                    return Err("mysql One-Click FK metadata is inconsistent".to_string());
                }
                foreign_key.columns.push(column);
            } else {
                foreign_keys.push(ActionForeignKeyFact {
                    constraint_schema,
                    constraint_name,
                    table_schema,
                    table_name,
                    referenced_table_schema,
                    referenced_table_name,
                    match_option,
                    update_rule,
                    delete_rule,
                    columns: vec![column],
                });
            }
        }
        Ok(foreign_keys)
    }
}

impl OneClickApplySession for LiveOneClickSession {
    fn acquire_advisory_lock(&mut self, key: &str, seconds: u32) -> Result<bool, String> {
        let acquired = self
            .conn
            .exec_first::<Option<i64>, _, _>("SELECT GET_LOCK(?, ?)", (key, seconds))
            .map_err(|err| format!("mysql One-Click lock acquisition failed: {err}"))?
            .flatten();
        Ok(acquired == Some(1))
    }

    fn release_advisory_lock(&mut self, key: &str) -> Result<(), String> {
        let released = self
            .conn
            .exec_first::<Option<i64>, _, _>("SELECT RELEASE_LOCK(?)", (key,))
            .map_err(|err| format!("mysql One-Click lock release failed: {err}"))?
            .flatten();
        if released == Some(1) {
            Ok(())
        } else {
            Err("mysql One-Click lock was not released by this session".to_string())
        }
    }

    fn read_action_facts(
        &mut self,
        action: &OneClickApplyAction,
    ) -> Result<ActionFactsDocument, String> {
        let tables = self.read_table_definitions(&action.schema)?;
        let foreign_keys = self.read_fk_facts(&action.schema)?;
        let names = action.tables.iter().cloned().collect::<BTreeSet<_>>();
        scoped_action_facts(action.action_type, &tables, &foreign_keys, &names)
            .map_err(|error| error.message.to_string())
    }

    fn execute_sql(&mut self, sql: &str) -> Result<(), String> {
        self.conn
            .query_drop(sql)
            .map_err(|err| format!("mysql One-Click action failed: {err}"))
    }
}

fn oneclick_contract_error_event(request: &Request, error: &OneClickContractError) -> Value {
    let mut event = protocol_error_event(
        request.request_id.clone(),
        error.code(),
        error.message,
    );
    event["applied_ordinals"] = json!(error.applied_ordinals());
    if error.outcome_indeterminate() {
        event["outcome_indeterminate"] = json!(true);
        event["indeterminate_ordinal"] = json!(error.indeterminate_ordinal());
    }
    if error.lock_release_failed() {
        event["lock_release_failed"] = json!(true);
    }
    event
}

pub(crate) fn oneclick_plan(request: &Request) -> Vec<Value> {
    let validated = match parse_oneclick_plan_request(request) {
        Ok(validated) => validated,
        Err(error) => return vec![oneclick_contract_error_event(request, &error)],
    };
    let mut session = match LiveOneClickSession::connect(&validated.endpoint) {
        Ok(session) => session,
        Err(error) => return vec![oneclick_contract_error_event(request, &error)],
    };
    match build_oneclick_plan(
        &mut session,
        &validated.endpoint,
        &validated.schema,
    ) {
        Ok(plan) => {
            let approval = oneclick_approval_artifact(&plan);
            vec![json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.plan",
                "success": true,
                "plan": plan,
                "approval": approval
            })]
        }
        Err(error) => vec![oneclick_contract_error_event(request, &error)],
    }
}

pub(crate) fn oneclick_legacy_preview_disabled(request: &Request) -> Vec<Value> {
    vec![oneclick_contract_error_event(
        request,
        &OneClickContractError::new(
            "oneclick_legacy_preview_disabled",
            "Legacy One-Click preview is disabled; use oneclick.plan.",
        ),
    )]
}

fn oneclick_apply_disabled_event(request: &Request) -> Value {
    oneclick_contract_error_event(
        request,
        &OneClickContractError::new(
            "oneclick_apply_disabled",
            "One-Click apply is disabled until both execution safety proofs are available.",
        ),
    )
}

pub(crate) fn oneclick_apply_with_session_factory<F>(
    request: &Request,
    exact_plan_enabled: bool,
    strong_fence_proven: bool,
    factory: F,
) -> Vec<Value>
where
    F: FnOnce(&ValidatedOneClickApplyRequest) -> Vec<Value>,
{
    let dry_run = request
        .payload
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    if dry_run {
        return oneclick_legacy_preview_disabled(request);
    }
    if !oneclick_apply_enabled(exact_plan_enabled, strong_fence_proven) {
        return vec![oneclick_apply_disabled_event(request)];
    }
    let validated = match parse_oneclick_apply_request(request) {
        Ok(validated) => validated,
        Err(error) => return vec![oneclick_contract_error_event(request, &error)],
    };
    factory(&validated)
}

pub(crate) fn preflight_streaming<F, R>(request: &Request, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    emit(phase_event(
        request,
        "preflight",
        "preflight checks started",
    ))
    .into_protocol_emit_result()?;
    let mut issues = preflight_issues(&request.payload);
    emit(phase_event(
        request,
        "preflight",
        "schema compatibility checks completed",
    ))
    .into_protocol_emit_result()?;
    emit(phase_event(request, "preflight", "checking target state"))
        .into_protocol_emit_result()?;
    issues.extend(live_preflight_issues(&request.payload));
    emit(phase_event(
        request,
        "preflight",
        "target state checks completed",
    ))
    .into_protocol_emit_result()?;

    for issue in &issues {
        emit(json!({
            "event": "issue",
            "request_id": request.request_id,
            "issue": issue
        }))
        .into_protocol_emit_result()?;
    }

    emit(phase_event(request, "preflight", "preflight result ready"))
        .into_protocol_emit_result()?;
    emit(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "preflight",
        "success": !issues.iter().any(|issue| issue.blocking),
        "issues": issues
    }))
    .into_protocol_emit_result()?;
    Ok(())
}

pub(crate) fn oneclick_run_streaming<F, R>(
    request: &Request,
    mut emit: F,
) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    if request.command == "oneclick.run" {
        for event in oneclick_legacy_preview_disabled(request) {
            emit(event).into_protocol_emit_result()?;
        }
        return Ok(());
    }
    let dry_run = request
        .payload
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    if !dry_run {
        emit(oneclick_apply_disabled_event(request)).into_protocol_emit_result()?;
        return Ok(());
    }

    emit(phase_event(
        request,
        "preflight",
        "one-click preflight started",
    ))
    .into_protocol_emit_result()?;
    emit(oneclick_progress_event(request, 5, "Pre-flight started"))
        .into_protocol_emit_result()?;
    let state = match oneclick_preflight_state(request) {
        Ok(state) => state,
        Err(err) => {
            emit(error_event(request, err)).into_protocol_emit_result()?;
            return Ok(());
        }
    };
    emit(oneclick_preflight_event(request, &state)).into_protocol_emit_result()?;
    emit(oneclick_progress_event(request, 20, "Pre-flight completed"))
        .into_protocol_emit_result()?;
    let mut run_issues = state.issues.clone();
    let payload_issue_offset = run_issues.len();
    run_issues.extend(oneclick_payload_issues(&request.payload));
    if run_issues.iter().any(|issue| issue.blocking) {
        emit(oneclick_final_result(
            request,
            &state.schema_name,
            false,
            &run_issues,
            &run_issues,
            vec!["Pre-flight blocked execution.".to_string()],
        ))
        .into_protocol_emit_result()?;
        return Ok(());
    }

    emit(phase_event(
        request,
        "analysis",
        "one-click analysis started",
    ))
    .into_protocol_emit_result()?;
    let analysis = oneclick_analysis_summary(&state.inspection, &run_issues);
    emit(json!({
        "event": "analysis",
        "request_id": request.request_id,
        "summary": analysis
    }))
    .into_protocol_emit_result()?;
    emit(oneclick_progress_event(request, 40, "Analysis completed"))
        .into_protocol_emit_result()?;

    emit(phase_event(
        request,
        "recommendation",
        "one-click recommendations ready",
    ))
    .into_protocol_emit_result()?;
    let charset_contracts = oneclick_charset_contracts_by_issue_index_with_offset(
        &request.payload,
        payload_issue_offset,
    );
    let recommendations =
        oneclick_recommendations(&run_issues, &state.schema_name, &charset_contracts);
    let recommendation_summary = oneclick_recommendation_summary(&recommendations);
    emit(json!({
        "event": "execution_plan",
        "request_id": request.request_id,
        "steps": recommendations,
        "summary": recommendation_summary
    }))
    .into_protocol_emit_result()?;
    emit(oneclick_progress_event(
        request,
        55,
        "Recommendations completed",
    ))
    .into_protocol_emit_result()?;

    emit(phase_event(
        request,
        "execution",
        "one-click execution started",
    ))
    .into_protocol_emit_result()?;
    let plan_payload = json!({
        "schema": state.schema_name,
        "steps": recommendations
    });
    let apply_plan = oneclick_apply_actions(&plan_payload);
    let outcome = oneclick_execute_stage(&state, &apply_plan, dry_run);
    let execution_success = outcome.fail_count == 0 && outcome.disallowed_fix_attempts.is_empty();
    let report_execution_log = outcome.log.clone();
    let report_fail_count = outcome.fail_count;
    let report_disallowed_count = outcome.disallowed_fix_attempts.len();
    let report_applied_count = outcome.applied_fixes.len();
    let execution_message = if dry_run {
        "Execution completed"
    } else if execution_success {
        "Execution completed"
    } else {
        "Execution completed with errors"
    };
    emit(json!({
        "event": "execution",
        "request_id": request.request_id,
        "dry_run": dry_run,
        "success_count": outcome.success_count,
        "fail_count": outcome.fail_count,
        "skip_count": outcome.skip_count,
        "disallowed_fix_attempts": outcome.disallowed_fix_attempts,
        "applied_fixes": outcome.applied_fixes,
        "log": outcome.log
    }))
    .into_protocol_emit_result()?;
    emit(oneclick_progress_event(request, 80, execution_message))
        .into_protocol_emit_result()?;

    emit(phase_event(
        request,
        "validation",
        "one-click validation started",
    ))
    .into_protocol_emit_result()?;
    let validation_issues = issues_from_inspect_result(inspect_live(&state.endpoint));
    let validation_success = validation_issues.is_empty()
        && execution_success
        && report_fail_count == 0
        && report_disallowed_count == 0;
    emit(json!({
        "event": "validation",
        "request_id": request.request_id,
        "all_fixed": validation_success,
        "remaining_issues": validation_issues.clone(),
        "applied_fix_count": report_applied_count
    }))
    .into_protocol_emit_result()?;
    emit(oneclick_progress_event(
        request,
        100,
        "Validation completed",
    ))
    .into_protocol_emit_result()?;
    emit(oneclick_final_result(
        request,
        &state.schema_name,
        validation_success,
        &run_issues,
        &validation_issues,
        report_execution_log,
    ))
    .into_protocol_emit_result()?;
    Ok(())
}

pub(crate) fn oneclick_preflight(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "preflight",
        "one-click preflight started",
    )];
    match oneclick_preflight_state(request) {
        Ok(state) => {
            events.push(oneclick_preflight_event(request, &state));
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.preflight",
                "success": !state.issues.iter().any(|issue| issue.blocking),
                "schema": state.schema_name,
                "checks": state.checks,
                "issues": state.issues
            }));
        }
        Err(err) => events.push(error_event(request, err)),
    }
    events
}

pub(crate) fn oneclick_analyze(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "analysis",
        "one-click analysis started",
    )];
    match oneclick_preflight_state(request) {
        Ok(state) => {
            let summary = oneclick_analysis_summary(&state.inspection, &state.issues);
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.analyze",
                "success": true,
                "schema": state.schema_name,
                "summary": summary,
                "issues": state.issues
            }));
        }
        Err(err) => events.push(error_event(request, err)),
    }
    events
}

pub(crate) fn oneclick_recommend(request: &Request) -> Vec<Value> {
    if request.command == "oneclick.recommend" {
        return oneclick_legacy_preview_disabled(request);
    }
    let mut events = vec![phase_event(
        request,
        "recommendation",
        "one-click recommendation started",
    )];
    let schema = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("");
    let issues = oneclick_payload_issues(&request.payload);
    let charset_contracts = oneclick_charset_contracts_by_issue_index(&request.payload);
    let recommendations = oneclick_recommendations(&issues, schema, &charset_contracts);
    let summary = oneclick_recommendation_summary(&recommendations);
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.recommend",
        "success": true,
        "steps": recommendations,
        "summary": summary
    }));
    events
}

pub(crate) fn oneclick_derive_charset_contracts(request: &Request) -> Vec<Value> {
    if request.command == "oneclick.derive_charset_contracts" {
        return oneclick_legacy_preview_disabled(request);
    }
    let mut events = vec![phase_event(
        request,
        "recommendation",
        "one-click charset contract derivation started",
    )];
    let mut schema_name = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let target_charset = request
        .payload
        .get("target_charset")
        .and_then(Value::as_str)
        .unwrap_or("utf8mb4");
    let target_collation = request
        .payload
        .get("target_collation")
        .and_then(Value::as_str)
        .unwrap_or("utf8mb4_0900_ai_ci");
    let mut issues = oneclick_payload_issues(&request.payload);
    let mut table_facts = oneclick_charset_table_facts_from_payload(&request.payload);
    let mut fk_facts = oneclick_charset_fk_facts_from_payload(&request.payload);
    if table_facts.is_empty() && oneclick_has_endpoint_payload(&request.payload) {
        match oneclick_endpoint(request).and_then(|(endpoint, endpoint_schema)| {
            let facts = oneclick_live_charset_facts(&endpoint, &endpoint_schema)?;
            Ok((endpoint_schema, facts))
        }) {
            Ok((endpoint_schema, (live_table_facts, live_fk_facts))) => {
                schema_name = endpoint_schema;
                table_facts = live_table_facts;
                fk_facts = live_fk_facts;
                if issues.is_empty() {
                    issues = oneclick_synthetic_charset_issues_from_facts(
                        &schema_name,
                        &table_facts,
                        &fk_facts,
                        target_charset,
                        target_collation,
                    );
                }
            }
            Err(err) => {
                events.push(error_event(request, err));
                return events;
            }
        }
    }
    let contracts = oneclick_derive_charset_contracts_from_facts(
        &issues,
        &schema_name,
        &table_facts,
        &fk_facts,
        target_charset,
        target_collation,
    );
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.derive_charset_contracts",
        "success": true,
        "schema": schema_name,
        "issues": issues,
        "contracts": contracts,
        "summary": {
            "total_issues": issues.len(),
            "derived_contracts": contracts.len(),
            "manual_review": issues.len().saturating_sub(contracts.len())
        }
    }));
    events
}

pub(crate) fn oneclick_apply_fixes(request: &Request) -> Vec<Value> {
    oneclick_apply_with_session_factory(
        request,
        ONECLICK_EXACT_PLAN_ENABLED,
        ONECLICK_STRONG_FENCE_PROVEN,
        |validated| {
            let mut session = match LiveOneClickSession::connect(&validated.endpoint) {
                Ok(session) => session,
                Err(_) => {
                    return vec![oneclick_contract_error_event(
                        request,
                        &OneClickContractError::new(
                            "oneclick_replan_failed",
                            "The approved One-Click target could not be opened.",
                        ),
                    )]
                }
            };
            match execute_approved_oneclick(&mut session, validated) {
                Ok(outcome) => vec![json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "oneclick.apply_fixes",
                    "success": true,
                    "dry_run": false,
                    "applied_ordinals": outcome.applied_ordinals
                })],
                Err(error) => vec![oneclick_contract_error_event(request, &error)],
            }
        },
    )
}

pub(crate) fn oneclick_validate(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "validation",
        "one-click validation started",
    )];
    match oneclick_endpoint(request) {
        Ok((endpoint, schema_name)) => {
            let issues = issues_from_inspect_result(inspect_live(&endpoint));
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.validate",
                "success": issues.is_empty(),
                "schema": schema_name,
                "remaining_issues": issues,
                "all_fixed": issues.is_empty()
            }));
        }
        Err(err) => events.push(error_event(request, err)),
    }
    events
}

pub(crate) fn oneclick_report(request: &Request) -> Vec<Value> {
    let schema = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("");
    let success = request
        .payload
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let pre_issues = oneclick_payload_issues(&request.payload);
    let remaining_issues = request
        .payload
        .get("remaining_issues")
        .and_then(Value::as_array)
        .map(|issues| {
            issues
                .iter()
                .filter_map(|issue| serde_json::from_value::<MigrationIssue>(issue.clone()).ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.report",
        "success": success,
        "report": oneclick_report_value(schema, success, &pre_issues, &remaining_issues, Vec::new())
    })]
}

#[derive(Debug, Clone)]
struct OneClickState {
    endpoint: Endpoint,
    schema_name: String,
    inspection: InspectionResult,
    checks: Vec<Value>,
    issues: Vec<MigrationIssue>,
}

fn oneclick_preflight_state(request: &Request) -> Result<OneClickState, String> {
    let (endpoint, schema_name) = oneclick_endpoint(request)?;
    let mut checks = Vec::new();
    let mut issues = Vec::new();
    if endpoint.engine != "mysql" {
        checks.push(oneclick_check(
            "MySQL engine",
            false,
            "error",
            "One-Click migration currently supports MySQL endpoints only.",
        ));
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: "connection".to_string(),
            message: "One-Click migration currently supports MySQL endpoints only.".to_string(),
            suggestion: "Use Cross-Engine Migration for PostgreSQL workflows.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        });
    } else {
        checks.push(oneclick_check(
            "MySQL engine",
            true,
            "info",
            "MySQL endpoint confirmed.",
        ));
    }

    if request
        .payload
        .get("backup_confirmed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        checks.push(oneclick_check(
            "Backup status",
            true,
            "info",
            "Backup confirmation was provided.",
        ));
    } else {
        checks.push(oneclick_check(
            "Backup status",
            false,
            "warning",
            "Backup confirmation was not provided.",
        ));
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "warning".to_string(),
            location: "backup".to_string(),
            message: "Backup confirmation was not provided.".to_string(),
            suggestion: "Confirm a restorable backup before running destructive fixes.".to_string(),
            blocking: false,
            table_name: None,
            column_name: None,
        });
    }

    match inspect_live(&endpoint) {
        Ok(inspection) => {
            checks.push(oneclick_check(
                "Schema inspect",
                true,
                "info",
                &format!("Inspected {} table(s).", inspection.schema.tables.len()),
            ));
            issues.extend(oneclick_issues_from_inspection(&inspection));
            Ok(OneClickState {
                endpoint,
                schema_name,
                inspection,
                checks,
                issues,
            })
        }
        Err(err) => {
            checks.push(oneclick_check("Schema inspect", false, "error", &err));
            issues.push(MigrationIssue {
                issue_type: None,
                severity: "error".to_string(),
                location: "schema".to_string(),
                message: err,
                suggestion: "Check database connection, schema, and inspection permissions."
                    .to_string(),
                blocking: true,
                table_name: None,
                column_name: None,
            });
            Ok(OneClickState {
                endpoint,
                schema_name,
                inspection: InspectionResult::default(),
                checks,
                issues,
            })
        }
    }
}

fn oneclick_endpoint(request: &Request) -> Result<(Endpoint, String), String> {
    let mut endpoint = request_endpoint(request)?;
    let schema_name = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|schema| !schema.is_empty())
        .map(ToString::to_string)
        .unwrap_or_else(|| endpoint_schema(&endpoint));
    if endpoint.engine == "mysql" {
        endpoint.database = schema_name.clone();
        endpoint.schema = None;
    } else {
        endpoint.schema = Some(schema_name.clone());
    }
    Ok((endpoint, schema_name))
}

fn oneclick_check(name: &str, passed: bool, severity: &str, message: &str) -> Value {
    json!({
        "name": name,
        "passed": passed,
        "severity": severity,
        "message": message
    })
}

fn oneclick_issues_from_inspection(inspection: &InspectionResult) -> Vec<MigrationIssue> {
    inspection
        .unsupported_objects
        .iter()
        .map(|object| MigrationIssue {
            issue_type: oneclick_deprecated_engine_marker(object)
                .map(|_| "deprecated_engine".to_string()),
            severity: "warning".to_string(),
            location: oneclick_deprecated_engine_marker(object)
                .map(|(table, _)| table.clone())
                .unwrap_or_else(|| object.clone()),
            message: oneclick_deprecated_engine_marker(object)
                .map(|(table, engine)| {
                    format!("Deprecated storage engine detected on table {table}: {engine}")
                })
                .unwrap_or_else(|| format!("Unsupported object detected: {object}")),
            suggestion: oneclick_deprecated_engine_marker(object)
                .map(|_| "Convert the table to InnoDB.".to_string())
                .unwrap_or_else(|| {
                    "Review this object manually before promoting One-Click migration.".to_string()
                }),
            blocking: false,
            table_name: oneclick_deprecated_engine_marker(object).map(|(table, _)| table),
            column_name: None,
        })
        .collect()
}

/// inspect 결과(성공/실패)를 검증용 MigrationIssue 목록으로 변환한다.
/// Ok → oneclick_issues_from_inspection, Err → 단일 validation-error MigrationIssue.
/// oneclick_run_streaming 과 oneclick_validate 에 중복돼 있던 fallback 블록을 하나로 통합한다.
fn issues_from_inspect_result(result: Result<InspectionResult, String>) -> Vec<MigrationIssue> {
    match result {
        Ok(inspection) => oneclick_issues_from_inspection(&inspection),
        Err(err) => vec![MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: "validation".to_string(),
            message: err,
            suggestion: "Check the database connection and rerun validation.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        }],
    }
}

fn oneclick_deprecated_engine_marker(object: &str) -> Option<(String, String)> {
    let marker = object.strip_prefix("deprecated_engine:")?;
    let (table, engine) = marker.rsplit_once(':')?;
    if table.is_empty()
        || engine.is_empty()
        || table.contains('\0')
        || engine.contains('\0')
    {
        return None;
    }
    Some((table.to_string(), engine.to_string()))
}

fn oneclick_payload_issues(payload: &Value) -> Vec<MigrationIssue> {
    payload
        .get("issues")
        .and_then(Value::as_array)
        .map(|issues| {
            issues
                .iter()
                .filter_map(|issue| serde_json::from_value::<MigrationIssue>(issue.clone()).ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default()
}

fn oneclick_preflight_event(request: &Request, state: &OneClickState) -> Value {
    json!({
        "event": "preflight",
        "request_id": request.request_id,
        "schema": state.schema_name,
        "passed": !state.issues.iter().any(|issue| issue.blocking),
        "checks": state.checks,
        "issues": state.issues
    })
}

fn oneclick_analysis_summary(inspection: &InspectionResult, issues: &[MigrationIssue]) -> Value {
    json!({
        "total_issues": issues.len(),
        "auto_fixable": 0,
        "manual_review": issues.len(),
        "table_count": inspection.schema.tables.len(),
        "unsupported_object_count": inspection.unsupported_objects.len()
    })
}

fn oneclick_recommendations(
    issues: &[MigrationIssue],
    schema: &str,
    charset_contracts: &BTreeMap<usize, Value>,
) -> Vec<Value> {
    issues
        .iter()
        .enumerate()
        .map(|(index, issue)| {
            let selected_option =
                oneclick_auto_fix_option(issue, schema, charset_contracts.get(&index))
                    .unwrap_or_else(|| oneclick_manual_option(issue));
            json!({
                "issue_index": index,
                "issue_type": issue.issue_type.clone().unwrap_or_else(|| "unknown".to_string()),
                "location": issue.location,
                "table_name": issue.table_name,
                "column_name": issue.column_name,
                "description": issue.message,
                "selected_option": selected_option
            })
        })
        .collect()
}

fn oneclick_recommendation_summary(steps: &[Value]) -> Value {
    let auto_fixable = steps
        .iter()
        .filter(|step| {
            step.get("selected_option")
                .and_then(|option| option.get("strategy"))
                .and_then(Value::as_str)
                .map(|strategy| strategy != "manual" && strategy != "skip")
                .unwrap_or(false)
        })
        .count();
    json!({
        "total_issues": steps.len(),
        "auto_fixable": auto_fixable,
        "manual_review": steps.len().saturating_sub(auto_fixable),
        "skip_recommended": 0
    })
}

fn oneclick_manual_option(issue: &MigrationIssue) -> Value {
    json!({
        "strategy": "manual",
        "label": "Manual review",
        "description": issue.suggestion,
        "sql_template": ""
    })
}

fn oneclick_auto_fix_option(
    issue: &MigrationIssue,
    schema: &str,
    charset_contract: Option<&Value>,
) -> Option<Value> {
    match issue.issue_type.as_deref() {
        Some("deprecated_engine") => {
            let table = issue.table_name.as_deref()?.trim();
            if table.is_empty() || schema.trim().is_empty() {
                return None;
            }
            Some(json!({
                "strategy": "engine_innodb",
                "label": "Convert table to InnoDB",
                "description": "Convert this deprecated storage engine table to InnoDB.",
                "sql_template": format!(
                    "ALTER TABLE {}.{} ENGINE=InnoDB;",
                    quote_ident("mysql", schema.trim()),
                    quote_ident("mysql", table),
                )
            }))
        }
        Some("charset_issue") => charset_contract.and_then(|contract| {
            oneclick_charset_fk_safe_option_from_payload(contract, schema).ok()
        }),
        _ => None,
    }
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
struct OneClickCharsetTableFact {
    table: String,
    charset: String,
    collation: String,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
struct OneClickCharsetFkFact {
    table: String,
    referenced_table: String,
}

fn oneclick_charset_table_facts_from_payload(payload: &Value) -> Vec<OneClickCharsetTableFact> {
    payload
        .get("table_facts")
        .and_then(Value::as_array)
        .map(|facts| {
            facts
                .iter()
                .filter_map(|fact| {
                    serde_json::from_value::<OneClickCharsetTableFact>(fact.clone()).ok()
                })
                .collect()
        })
        .unwrap_or_default()
}

fn oneclick_charset_fk_facts_from_payload(payload: &Value) -> Vec<OneClickCharsetFkFact> {
    payload
        .get("foreign_key_facts")
        .and_then(Value::as_array)
        .map(|facts| {
            facts
                .iter()
                .filter_map(|fact| {
                    serde_json::from_value::<OneClickCharsetFkFact>(fact.clone()).ok()
                })
                .collect()
        })
        .unwrap_or_default()
}

fn oneclick_has_endpoint_payload(payload: &Value) -> bool {
    ["connection", "endpoint", "source", "target"]
        .iter()
        .any(|key| payload.get(*key).is_some())
}

fn oneclick_live_charset_facts(
    endpoint: &Endpoint,
    schema: &str,
) -> Result<(Vec<OneClickCharsetTableFact>, Vec<OneClickCharsetFkFact>), String> {
    if endpoint.engine != "mysql" {
        return Err("oneclick.derive_charset_contracts currently supports MySQL only".to_string());
    }
    let opts = mysql_opts(endpoint);
    let pool = mysql::Pool::new(opts).map_err(|err| format!("mysql pool error: {err}"))?;
    let mut conn = pool
        .get_conn()
        .map_err(|err| format!("mysql connection error: {err}"))?;
    let table_facts = conn
        .exec_map(
            "SELECT t.TABLE_NAME, ccsa.CHARACTER_SET_NAME, t.TABLE_COLLATION \
             FROM information_schema.TABLES t \
             JOIN information_schema.COLLATION_CHARACTER_SET_APPLICABILITY ccsa \
             ON t.TABLE_COLLATION = ccsa.COLLATION_NAME \
             WHERE t.TABLE_SCHEMA = ? AND t.TABLE_TYPE = 'BASE TABLE' \
             ORDER BY t.TABLE_NAME",
            (schema,),
            |(table, charset, collation): (String, String, String)| OneClickCharsetTableFact {
                table,
                charset,
                collation,
            },
        )
        .map_err(|err| format!("mysql charset fact inspect error: {err}"))?;
    let fk_facts = conn
        .exec_map(
            "SELECT TABLE_NAME, REFERENCED_TABLE_NAME \
             FROM information_schema.KEY_COLUMN_USAGE \
             WHERE TABLE_SCHEMA = ? AND REFERENCED_TABLE_NAME IS NOT NULL \
             GROUP BY TABLE_NAME, REFERENCED_TABLE_NAME \
             ORDER BY TABLE_NAME, REFERENCED_TABLE_NAME",
            (schema,),
            |(table, referenced_table): (String, String)| OneClickCharsetFkFact {
                table,
                referenced_table,
            },
        )
        .map_err(|err| format!("mysql charset FK fact inspect error: {err}"))?;
    Ok((table_facts, fk_facts))
}

fn oneclick_synthetic_charset_issues_from_facts(
    schema: &str,
    table_facts: &[OneClickCharsetTableFact],
    fk_facts: &[OneClickCharsetFkFact],
    target_charset: &str,
    target_collation: &str,
) -> Vec<MigrationIssue> {
    let mut seen_groups = BTreeSet::new();
    let mut issues = Vec::new();
    for fact in table_facts {
        let candidate = MigrationIssue {
            issue_type: Some("charset_issue".to_string()),
            severity: "warning".to_string(),
            location: format!("{schema}.{}", fact.table),
            message: "Table uses a legacy charset/collation.".to_string(),
            suggestion: "Convert table charset/collation after FK-safe review.".to_string(),
            blocking: false,
            table_name: Some(fact.table.clone()),
            column_name: None,
        };
        let contracts = oneclick_derive_charset_contracts_from_facts(
            std::slice::from_ref(&candidate),
            schema,
            table_facts,
            fk_facts,
            target_charset,
            target_collation,
        );
        let Some(contract) = contracts.first() else {
            continue;
        };
        let tables = contract
            .get("tables")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(ToString::to_string)
            .collect::<Vec<_>>();
        if tables.is_empty() {
            continue;
        }
        let group_key = tables.join("\0");
        if !seen_groups.insert(group_key) {
            continue;
        }
        let table_name = contract
            .get("fk_order")
            .and_then(Value::as_array)
            .and_then(|values| values.first())
            .and_then(Value::as_str)
            .unwrap_or(&fact.table)
            .to_string();
        issues.push(MigrationIssue {
            location: format!("{schema}.{table_name}"),
            table_name: Some(table_name),
            ..candidate
        });
    }
    issues
}

fn oneclick_derive_charset_contracts_from_facts(
    issues: &[MigrationIssue],
    schema: &str,
    table_facts: &[OneClickCharsetTableFact],
    fk_facts: &[OneClickCharsetFkFact],
    target_charset: &str,
    target_collation: &str,
) -> Vec<Value> {
    let Ok(schema) = oneclick_safe_charset_identifier(schema, "schema") else {
        return Vec::new();
    };
    let Ok(target_charset) = oneclick_safe_charset_token(Some(target_charset), "target_charset")
    else {
        return Vec::new();
    };
    let Ok(target_collation) =
        oneclick_safe_charset_token(Some(target_collation), "target_collation")
    else {
        return Vec::new();
    };
    let table_by_name = table_facts
        .iter()
        .map(|fact| (fact.table.as_str(), fact))
        .collect::<BTreeMap<_, _>>();

    issues
        .iter()
        .enumerate()
        .filter_map(|(issue_index, issue)| {
            if issue.issue_type.as_deref() != Some("charset_issue") || issue.blocking {
                return None;
            }
            let table = issue.table_name.as_deref()?.trim();
            oneclick_safe_charset_identifier(table, "table").ok()?;
            let closure = oneclick_charset_fk_closure(table, &table_by_name, fk_facts)?;
            let fk_order = oneclick_charset_fk_order(&closure, fk_facts)?;
            let mut before_charset: Option<&str> = None;
            let mut before_collation: Option<&str> = None;
            for table in &fk_order {
                let fact = *table_by_name.get(table.as_str())?;
                oneclick_safe_charset_identifier(&fact.table, "table").ok()?;
                let charset = fact.charset.trim();
                let collation = fact.collation.trim();
                if charset.is_empty() || collation.is_empty() {
                    return None;
                }
                if charset.eq_ignore_ascii_case(&target_charset)
                    && collation.eq_ignore_ascii_case(&target_collation)
                {
                    return None;
                }
                match (before_charset, before_collation) {
                    (Some(existing_charset), Some(existing_collation))
                        if !charset.eq_ignore_ascii_case(existing_charset)
                            || !collation.eq_ignore_ascii_case(existing_collation) =>
                    {
                        return None;
                    }
                    (None, None) => {
                        before_charset = Some(charset);
                        before_collation = Some(collation);
                    }
                    _ => {}
                }
            }

            let rollback_sql = fk_order
                .iter()
                .rev()
                .map(|table| {
                    let fact = *table_by_name.get(table.as_str())?;
                    Some(format!(
                        "ALTER TABLE {}.{} CONVERT TO CHARACTER SET {} COLLATE {};",
                        quote_ident("mysql", &schema),
                        quote_ident("mysql", table),
                        fact.charset.trim(),
                        fact.collation.trim()
                    ))
                })
                .collect::<Option<Vec<_>>>()?;

            Some(json!({
                "issue_index": issue_index,
                "tables": fk_order,
                "fk_order": fk_order,
                "target_charset": target_charset,
                "target_collation": target_collation,
                "rollback_sql": rollback_sql
            }))
        })
        .collect()
}

fn oneclick_charset_fk_closure(
    seed_table: &str,
    table_by_name: &BTreeMap<&str, &OneClickCharsetTableFact>,
    fk_facts: &[OneClickCharsetFkFact],
) -> Option<BTreeSet<String>> {
    table_by_name.get(seed_table)?;
    let mut closure = BTreeSet::from([seed_table.to_string()]);
    loop {
        let before_len = closure.len();
        for fk in fk_facts {
            if closure.contains(&fk.table) || closure.contains(&fk.referenced_table) {
                table_by_name.get(fk.table.as_str())?;
                table_by_name.get(fk.referenced_table.as_str())?;
                oneclick_safe_charset_identifier(&fk.table, "table").ok()?;
                oneclick_safe_charset_identifier(&fk.referenced_table, "table").ok()?;
                closure.insert(fk.table.clone());
                closure.insert(fk.referenced_table.clone());
            }
        }
        if closure.len() == before_len {
            return Some(closure);
        }
    }
}

fn oneclick_charset_fk_order(
    closure: &BTreeSet<String>,
    fk_facts: &[OneClickCharsetFkFact],
) -> Option<Vec<String>> {
    let mut remaining = closure.clone();
    let mut ordered = Vec::new();
    while !remaining.is_empty() {
        let next = remaining.iter().find_map(|table| {
            let has_unresolved_parent = fk_facts.iter().any(|fk| {
                fk.table == *table
                    && closure.contains(&fk.referenced_table)
                    && remaining.contains(&fk.referenced_table)
            });
            if has_unresolved_parent {
                None
            } else {
                Some(table.clone())
            }
        })?;
        remaining.remove(&next);
        ordered.push(next);
    }
    Some(ordered)
}

fn oneclick_charset_contracts_by_issue_index(payload: &Value) -> BTreeMap<usize, Value> {
    oneclick_charset_contracts_by_issue_index_with_offset(payload, 0)
}

fn oneclick_charset_contracts_by_issue_index_with_offset(
    payload: &Value,
    offset: usize,
) -> BTreeMap<usize, Value> {
    payload
        .get("charset_contracts")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|contract| {
            let index = contract.get("issue_index").and_then(Value::as_u64)? as usize;
            Some((index + offset, contract.clone()))
        })
        .collect()
}

fn oneclick_charset_fk_safe_option_from_payload(
    payload: &Value,
    schema: &str,
) -> Result<Value, String> {
    let schema = oneclick_safe_charset_identifier(schema, "schema")?;
    let tables = oneclick_required_string_list(payload.get("tables"), "tables")?;
    let fk_order = oneclick_required_string_list(payload.get("fk_order"), "fk_order")?;
    let target_charset = oneclick_safe_charset_token(
        payload.get("target_charset").and_then(Value::as_str),
        "target_charset",
    )?;
    let target_collation = oneclick_safe_charset_token(
        payload.get("target_collation").and_then(Value::as_str),
        "target_collation",
    )?;
    let rollback_sql = oneclick_required_string_list(payload.get("rollback_sql"), "rollback_sql")?;

    if tables.is_empty() {
        return Err("tables must not be empty".to_string());
    }
    if rollback_sql.is_empty() {
        return Err("rollback_sql must not be empty".to_string());
    }
    for table in &tables {
        oneclick_safe_charset_identifier(table, "table")?;
    }
    for table in &fk_order {
        oneclick_safe_charset_identifier(table, "fk_order")?;
    }
    let table_set: BTreeSet<_> = tables.iter().cloned().collect();
    let fk_order_set: BTreeSet<_> = fk_order.iter().cloned().collect();
    if table_set != fk_order_set {
        return Err("fk_order must cover the same tables as tables".to_string());
    }

    let sql = fk_order
        .iter()
        .map(|table| {
            format!(
                "ALTER TABLE {}.{} CONVERT TO CHARACTER SET {} COLLATE {};",
                quote_ident("mysql", &schema),
                quote_ident("mysql", table),
                target_charset,
                target_collation
            )
        })
        .collect::<Vec<_>>();

    Ok(json!({
        "strategy": "charset_collation_fk_safe",
        "label": "Convert table charset/collation with FK-safe ordering",
        "description": "Convert the FK-connected table set to the explicit target charset/collation.",
        "tables": tables,
        "fk_order": fk_order,
        "target_charset": target_charset,
        "target_collation": target_collation,
        "sql": sql,
        "rollback_sql": rollback_sql
    }))
}

fn oneclick_required_string_list(
    value: Option<&Value>,
    label: &str,
) -> Result<Vec<String>, String> {
    let Some(values) = value.and_then(Value::as_array) else {
        return Err(format!("{label} must be an array"));
    };
    values
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::trim)
                .filter(|text| !text.is_empty())
                .map(ToString::to_string)
                .ok_or_else(|| format!("{label} must contain non-empty strings"))
        })
        .collect()
}

fn oneclick_safe_charset_identifier(value: &str, label: &str) -> Result<String, String> {
    let value = value.trim();
    if !value.starts_with("tf_oneclick_")
        || !value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
    {
        return Err(format!("{label} must use a safe tf_oneclick_ identifier"));
    }
    Ok(value.to_string())
}

fn oneclick_safe_charset_token(value: Option<&str>, label: &str) -> Result<String, String> {
    let Some(value) = value.map(str::trim).filter(|value| !value.is_empty()) else {
        return Err(format!("{label} is required"));
    };
    if !value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
    {
        return Err(format!("{label} must be a safe token"));
    }
    Ok(value.to_string())
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct LegacyOneClickApplyAction {
    issue_type: String,
    strategy: String,
    schema: String,
    table: String,
    sql: String,
    tables: Vec<String>,
    fk_order: Vec<String>,
    sql_statements: Vec<String>,
    rollback_sql: Vec<String>,
    target_charset: Option<String>,
    target_collation: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OneClickApplyPlan {
    actions: Vec<LegacyOneClickApplyAction>,
    skipped: usize,
    disallowed: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct LegacyOneClickApplyOutcome {
    success_count: usize,
    fail_count: usize,
    skip_count: usize,
    disallowed_fix_attempts: Vec<String>,
    log: Vec<String>,
    applied_fixes: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code)]
struct OneClickDryRunPreview {
    planned_fixes: Vec<Value>,
    skipped: usize,
    disallowed: Vec<String>,
}

/// One-Click step 의 공통 분류 결과.
/// apply(real) 와 dry-run preview 가 공유하는 per-step 판정만 담는다.
/// real-apply 전용인 sql_template 불일치 검사는 여기 포함하지 않고
/// oneclick_apply_actions 후처리에 남긴다(preview 출력 불변 보존).
enum OneClickStepClassification {
    Skip,
    Disallowed(String),
    Charset(Value),
    Engine { table: String, sql: String },
}

fn classify_oneclick_step(step: &Value, schema: &str) -> OneClickStepClassification {
    let issue_type = step
        .get("issue_type")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let selected = step.get("selected_option").unwrap_or(&Value::Null);
    let strategy = selected
        .get("strategy")
        .and_then(Value::as_str)
        .unwrap_or("manual");

    if strategy == "manual" || strategy == "skip" {
        return OneClickStepClassification::Skip;
    }
    if issue_type == "charset_issue" && strategy == "charset_collation_fk_safe" {
        return match oneclick_charset_fk_safe_option_from_payload(selected, schema) {
            Ok(option) => OneClickStepClassification::Charset(option),
            Err(_) => OneClickStepClassification::Disallowed(format!("{issue_type}:{strategy}")),
        };
    }
    if issue_type == "deprecated_engine" && strategy == "engine_innodb" {
        let Some(table) = oneclick_apply_step_table(step, schema) else {
            return OneClickStepClassification::Skip;
        };
        let sql = format!(
            "ALTER TABLE {}.{} ENGINE=InnoDB;",
            quote_ident("mysql", schema),
            quote_ident("mysql", &table),
        );
        return OneClickStepClassification::Engine { table, sql };
    }
    OneClickStepClassification::Disallowed(format!("{issue_type}:{strategy}"))
}

fn oneclick_apply_actions(payload: &Value) -> OneClickApplyPlan {
    let schema = payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let mut actions = Vec::new();
    let mut skipped = 0usize;
    let mut disallowed = Vec::new();

    for step in payload
        .get("steps")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        match classify_oneclick_step(step, schema) {
            OneClickStepClassification::Skip => skipped += 1,
            OneClickStepClassification::Disallowed(reason) => disallowed.push(reason),
            OneClickStepClassification::Charset(option) => {
                let tables = oneclick_required_string_list(option.get("tables"), "tables")
                    .unwrap_or_default();
                let fk_order = oneclick_required_string_list(option.get("fk_order"), "fk_order")
                    .unwrap_or_default();
                let sql_statements =
                    oneclick_required_string_list(option.get("sql"), "sql").unwrap_or_default();
                let rollback_sql =
                    oneclick_required_string_list(option.get("rollback_sql"), "rollback_sql")
                        .unwrap_or_default();
                actions.push(LegacyOneClickApplyAction {
                    issue_type: "charset_issue".to_string(),
                    strategy: "charset_collation_fk_safe".to_string(),
                    schema: schema.to_string(),
                    table: tables.first().cloned().unwrap_or_default(),
                    sql: sql_statements.first().cloned().unwrap_or_default(),
                    tables,
                    fk_order,
                    sql_statements,
                    rollback_sql,
                    target_charset: option
                        .get("target_charset")
                        .and_then(Value::as_str)
                        .map(ToString::to_string),
                    target_collation: option
                        .get("target_collation")
                        .and_then(Value::as_str)
                        .map(ToString::to_string),
                });
            }
            OneClickStepClassification::Engine { table, sql } => {
                // real-apply 전용: 클라이언트가 보낸 sql_template 이 서버 산출 SQL 과
                // 불일치하면 disallowed 로 거부한다(preview 경로에는 적용하지 않음).
                let selected = step.get("selected_option").unwrap_or(&Value::Null);
                if selected
                    .get("sql_template")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|template| !template.is_empty() && *template != sql)
                    .is_some()
                {
                    disallowed.push("deprecated_engine:engine_innodb:sql_mismatch".to_string());
                    continue;
                }
                actions.push(LegacyOneClickApplyAction {
                    issue_type: "deprecated_engine".to_string(),
                    strategy: "engine_innodb".to_string(),
                    schema: schema.to_string(),
                    table: table.clone(),
                    sql: sql.clone(),
                    tables: vec![table],
                    fk_order: Vec::new(),
                    sql_statements: vec![sql],
                    rollback_sql: Vec::new(),
                    target_charset: None,
                    target_collation: None,
                });
            }
        }
    }

    OneClickApplyPlan {
        actions,
        skipped,
        disallowed,
    }
}

#[allow(dead_code)]
fn oneclick_dry_run_preview_fixes(payload: &Value) -> OneClickDryRunPreview {
    let schema = payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let mut planned_fixes = Vec::new();
    let mut skipped = 0usize;
    let mut disallowed = Vec::new();

    for step in payload
        .get("steps")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        match classify_oneclick_step(step, schema) {
            OneClickStepClassification::Skip => skipped += 1,
            OneClickStepClassification::Disallowed(reason) => disallowed.push(reason),
            OneClickStepClassification::Charset(mut plan) => {
                if let Some(object) = plan.as_object_mut() {
                    object.insert("issue_type".to_string(), json!("charset_issue"));
                    object.insert("schema".to_string(), json!(schema));
                    object.insert("dry_run".to_string(), json!(true));
                    object.insert("success".to_string(), json!(false));
                }
                planned_fixes.push(plan);
            }
            OneClickStepClassification::Engine { table, sql } => {
                planned_fixes.push(json!({
                    "issue_type": "deprecated_engine",
                    "strategy": "engine_innodb",
                    "schema": schema,
                    "table": table,
                    "sql": sql,
                    "dry_run": true,
                    "success": false
                }));
            }
        }
    }

    OneClickDryRunPreview {
        planned_fixes,
        skipped,
        disallowed,
    }
}

fn oneclick_execute_apply_plan<A: MigrationAdapter>(
    plan: &OneClickApplyPlan,
    adapter: &mut A,
) -> LegacyOneClickApplyOutcome {
    let mut success_count = 0usize;
    let mut fail_count = 0usize;
    let mut log = Vec::new();
    let mut applied_fixes = Vec::new();

    for action in &plan.actions {
        let mut action_error = None;
        for sql in &action.sql_statements {
            match adapter.execute_sql(sql) {
                Ok(()) => {
                    log.push(format!("APPLIED: {sql}"));
                }
                Err(err) => {
                    log.push(format!("FAILED: {sql}: {err}"));
                    action_error = Some(err);
                    break;
                }
            }
        }

        if let Some(err) = action_error {
            fail_count += 1;
            applied_fixes.push(oneclick_applied_fix_payload(action, false, Some(&err)));
        } else {
            success_count += 1;
            applied_fixes.push(oneclick_applied_fix_payload(action, true, None));
        }
    }

    LegacyOneClickApplyOutcome {
        success_count,
        fail_count,
        skip_count: plan.skipped,
        disallowed_fix_attempts: Vec::new(),
        log,
        applied_fixes,
    }
}

/// One-Click 실행 단계: dry-run / disallowed / no-action / live-apply 4분기를 처리해
/// 타입드 OneClickApplyOutcome 로 반환한다. 기존 익명 6-tuple 을 대체한다(동작 보존).
fn oneclick_execute_stage(
    state: &OneClickState,
    apply_plan: &OneClickApplyPlan,
    dry_run: bool,
) -> LegacyOneClickApplyOutcome {
    if dry_run {
        LegacyOneClickApplyOutcome {
            success_count: 0,
            fail_count: 0,
            skip_count: apply_plan.actions.len() + apply_plan.skipped,
            disallowed_fix_attempts: apply_plan.disallowed.clone(),
            log: vec!["DRY-RUN: no database changes were executed.".to_string()],
            applied_fixes: Vec::new(),
        }
    } else if !apply_plan.disallowed.is_empty() {
        LegacyOneClickApplyOutcome {
            success_count: 0,
            fail_count: apply_plan.disallowed.len(),
            skip_count: apply_plan.skipped,
            disallowed_fix_attempts: apply_plan.disallowed.clone(),
            log: vec!["Disallowed One-Click automatic fix attempt blocked.".to_string()],
            applied_fixes: Vec::new(),
        }
    } else if apply_plan.actions.is_empty() {
        LegacyOneClickApplyOutcome {
            success_count: 0,
            fail_count: 0,
            skip_count: apply_plan.skipped,
            disallowed_fix_attempts: Vec::new(),
            log: vec!["No automatic Rust Core fixes are currently required.".to_string()],
            applied_fixes: Vec::new(),
        }
    } else {
        match LiveAdapter::connect(&state.endpoint) {
            Ok(mut adapter) => oneclick_execute_apply_plan(apply_plan, &mut adapter),
            Err(err) => LegacyOneClickApplyOutcome {
                success_count: 0,
                fail_count: apply_plan.actions.len(),
                skip_count: apply_plan.skipped,
                disallowed_fix_attempts: Vec::new(),
                log: vec![format!(
                    "FAILED: unable to connect for One-Click fixes: {err}"
                )],
                applied_fixes: Vec::new(),
            },
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OneClickPayloadShape {
    CharsetCollationFkSafe,
    SingleTable,
}

fn classify_oneclick_payload_shape(action: &LegacyOneClickApplyAction) -> OneClickPayloadShape {
    if action.issue_type == "charset_issue" && action.strategy == "charset_collation_fk_safe" {
        OneClickPayloadShape::CharsetCollationFkSafe
    } else {
        OneClickPayloadShape::SingleTable
    }
}

fn oneclick_applied_fix_payload(
    action: &LegacyOneClickApplyAction,
    success: bool,
    error: Option<&str>,
) -> Value {
    let mut payload = match classify_oneclick_payload_shape(action) {
        OneClickPayloadShape::CharsetCollationFkSafe => json!({
            "issue_type": action.issue_type,
            "strategy": action.strategy,
            "schema": action.schema,
            "tables": action.tables,
            "target_charset": action.target_charset,
            "target_collation": action.target_collation,
            "sql": action.sql_statements,
            "rollback_sql": action.rollback_sql,
            "fk_order": action.fk_order,
            "success": success
        }),
        OneClickPayloadShape::SingleTable => json!({
            "issue_type": action.issue_type,
            "strategy": action.strategy,
            "schema": action.schema,
            "table": action.table,
            "sql": action.sql,
            "success": success,
            "rows_affected": 0
        }),
    };
    if let Some(error) = error {
        payload["error"] = json!(error);
    }
    payload
}

fn oneclick_apply_step_table(step: &Value, schema: &str) -> Option<String> {
    if schema.trim().is_empty() {
        return None;
    }
    if let Some(table) = step
        .get("table_name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|table| !table.is_empty())
    {
        return Some(table.to_string());
    }

    let location = step.get("location").and_then(Value::as_str)?.trim();
    let mut parts = location.split('.');
    match (parts.next(), parts.next(), parts.next()) {
        (Some(location_schema), Some(table), None)
            if location_schema == schema && !table.is_empty() =>
        {
            Some(table.to_string())
        }
        _ => None,
    }
}

fn oneclick_progress_event(request: &Request, percent: u64, message: &str) -> Value {
    json!({
        "event": "progress",
        "request_id": request.request_id,
        "percent": percent,
        "message": message
    })
}

fn oneclick_final_result(
    request: &Request,
    schema: &str,
    success: bool,
    pre_issues: &[MigrationIssue],
    remaining_issues: &[MigrationIssue],
    execution_log: Vec<String>,
) -> Value {
    json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.run",
        "success": success,
        "report": oneclick_report_value(schema, success, pre_issues, remaining_issues, execution_log)
    })
}

fn oneclick_report_value(
    schema: &str,
    success: bool,
    pre_issues: &[MigrationIssue],
    remaining_issues: &[MigrationIssue],
    execution_log: Vec<String>,
) -> Value {
    json!({
        "schema": schema,
        "started_at": current_unix_seconds().to_string(),
        "completed_at": current_unix_seconds().to_string(),
        "pre_issue_count": pre_issues.len(),
        "post_issue_count": remaining_issues.len(),
        "fixed_issues": [],
        "remaining_issues": remaining_issues,
        "new_issues": [],
        "success": success,
        "execution_log": execution_log,
        "duration_seconds": 0.0
    })
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    use serde_json::json;
    use std::collections::{BTreeMap, VecDeque};
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::RecordingAdapter;

    #[test]
    fn oneclick_issues_classify_deprecated_engine_marker_as_auto_fixable() {
        let inspection = InspectionResult {
            schema: NormalizedSchema {
                tables: vec![NormalizedTable {
                    name: "legacy_table".to_string(),
                    columns: Vec::new(),
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                }],
            },
            unsupported_objects: vec!["deprecated_engine:legacy_table:MyISAM".to_string()],
        };

        let issues = oneclick_issues_from_inspection(&inspection);
        let charset_contracts = BTreeMap::new();
        let recommendations = oneclick_recommendations(&issues, "app", &charset_contracts);
        let summary = oneclick_recommendation_summary(&recommendations);

        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].issue_type.as_deref(), Some("deprecated_engine"));
        assert_eq!(issues[0].table_name.as_deref(), Some("legacy_table"));
        assert_eq!(summary["auto_fixable"], 1);
        assert_eq!(
            recommendations[0]["selected_option"]["strategy"],
            "engine_innodb"
        );
    }

    #[test]
    fn oneclick_live_inspection_does_not_synthesize_int_display_width_skip() {
        let inspection = InspectionResult {
            schema: NormalizedSchema {
                tables: vec![NormalizedTable {
                    name: "orders".to_string(),
                    columns: Vec::new(),
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                }],
            },
            unsupported_objects: vec!["int_display_width:orders.id".to_string()],
        };

        let issues = oneclick_issues_from_inspection(&inspection);
        let charset_contracts = BTreeMap::new();
        let recommendations = oneclick_recommendations(&issues, "app", &charset_contracts);
        let summary = oneclick_recommendation_summary(&recommendations);

        assert_eq!(issues.len(), 1);
        assert_ne!(issues[0].issue_type.as_deref(), Some("int_display_width"));
        assert_eq!(summary["auto_fixable"], 0);
        assert_eq!(summary["skip_recommended"], 0);
        assert_eq!(recommendations[0]["selected_option"]["strategy"], "manual");
    }

    #[test]
    fn oneclick_charset_contract_builds_fk_safe_option() {
        let option = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap();

        assert_eq!(option["strategy"], "charset_collation_fk_safe");
        assert_eq!(option["target_charset"], "utf8mb4");
        assert_eq!(option["target_collation"], "utf8mb4_0900_ai_ci");
        assert_eq!(
            option["tables"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            option["fk_order"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            option["sql"],
            json!([
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            ])
        );
        assert_eq!(option["rollback_sql"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn oneclick_derives_charset_contract_from_safe_fk_facts() {
        let issues = vec![MigrationIssue {
            issue_type: Some("charset_issue".to_string()),
            severity: "warning".to_string(),
            location: "tf_oneclick_charset.tf_oneclick_parent".to_string(),
            message: "Table uses a legacy charset.".to_string(),
            suggestion: "Convert table charset/collation after FK-safe review.".to_string(),
            blocking: false,
            table_name: Some("tf_oneclick_parent".to_string()),
            column_name: None,
        }];
        let table_facts = vec![
            OneClickCharsetTableFact {
                table: "tf_oneclick_parent".to_string(),
                charset: "utf8mb3".to_string(),
                collation: "utf8mb3_general_ci".to_string(),
            },
            OneClickCharsetTableFact {
                table: "tf_oneclick_child".to_string(),
                charset: "utf8mb3".to_string(),
                collation: "utf8mb3_general_ci".to_string(),
            },
        ];
        let fk_facts = vec![OneClickCharsetFkFact {
            table: "tf_oneclick_child".to_string(),
            referenced_table: "tf_oneclick_parent".to_string(),
        }];

        let contracts = oneclick_derive_charset_contracts_from_facts(
            &issues,
            "tf_oneclick_charset",
            &table_facts,
            &fk_facts,
            "utf8mb4",
            "utf8mb4_0900_ai_ci",
        );

        assert_eq!(contracts.len(), 1);
        assert_eq!(contracts[0]["issue_index"], 0);
        assert_eq!(
            contracts[0]["tables"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            contracts[0]["fk_order"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(contracts[0]["target_charset"], "utf8mb4");
        assert_eq!(contracts[0]["target_collation"], "utf8mb4_0900_ai_ci");
        assert_eq!(
            contracts[0]["rollback_sql"],
            json!([
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
            ])
        );
    }

    #[test]
    fn oneclick_derives_no_charset_contract_for_unsafe_or_incomplete_facts() {
        let issues = vec![MigrationIssue {
            issue_type: Some("charset_issue".to_string()),
            severity: "warning".to_string(),
            location: "prod.customer".to_string(),
            message: "Table uses a legacy charset.".to_string(),
            suggestion: "Convert table charset/collation after FK-safe review.".to_string(),
            blocking: false,
            table_name: Some("customer".to_string()),
            column_name: None,
        }];
        let table_facts = vec![OneClickCharsetTableFact {
            table: "customer".to_string(),
            charset: "utf8mb3".to_string(),
            collation: "utf8mb3_general_ci".to_string(),
        }];

        let contracts = oneclick_derive_charset_contracts_from_facts(
            &issues,
            "prod",
            &table_facts,
            &[],
            "utf8mb4",
            "utf8mb4_0900_ai_ci",
        );

        assert!(contracts.is_empty());
    }

    #[test]
    fn oneclick_charset_contract_rejects_missing_target() {
        let err = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent"],
                "fk_order": ["tf_oneclick_parent"],
                "target_charset": "utf8mb4",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap_err();

        assert!(err.contains("target_collation"));
    }

    #[test]
    fn oneclick_charset_contract_rejects_incomplete_fk_order() {
        let err = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                "fk_order": ["tf_oneclick_parent"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap_err();

        assert!(err.contains("fk_order"));
    }

    #[test]
    fn oneclick_charset_contract_rejects_unsafe_schema_or_table() {
        let unsafe_schema = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent"],
                "fk_order": ["tf_oneclick_parent"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "prod",
        )
        .unwrap_err();
        assert!(unsafe_schema.contains("schema"));

        let unsafe_table = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["users"],
                "fk_order": ["users"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`users` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap_err();
        assert!(unsafe_table.contains("table"));
    }

    #[test]
    fn oneclick_apply_actions_accepts_only_engine_innodb_steps() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "app",
            "steps": [{
                "issue_type": "deprecated_engine",
                "location": "app.legacy_table",
                "selected_option": {
                    "strategy": "engine_innodb",
                    "sql_template": "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
                }
            }, {
                "issue_type": "charset_issue",
                "location": "app.users.name",
                "selected_option": {
                    "strategy": "manual",
                    "sql_template": ""
                }
            }]
        }));

        assert_eq!(plan.actions.len(), 1);
        assert_eq!(plan.skipped, 1);
        assert_eq!(plan.disallowed.len(), 0);
        assert_eq!(plan.actions[0].issue_type, "deprecated_engine");
        assert_eq!(plan.actions[0].strategy, "engine_innodb");
        assert_eq!(plan.actions[0].schema, "app");
        assert_eq!(plan.actions[0].table, "legacy_table");
        assert_eq!(
            plan.actions[0].sql,
            "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
        );
    }

    #[test]
    fn oneclick_apply_plan_executes_engine_innodb_sql() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "app",
            "steps": [{
                "issue_type": "deprecated_engine",
                "location": "app.legacy_table",
                "selected_option": {
                    "strategy": "engine_innodb",
                    "sql_template": "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
                }
            }]
        }));
        let mut adapter = RecordingAdapter::default();

        let outcome = oneclick_execute_apply_plan(&plan, &mut adapter);

        assert_eq!(
            adapter.executed_sql,
            vec!["ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"]
        );
        assert_eq!(outcome.success_count, 1);
        assert_eq!(outcome.fail_count, 0);
        assert_eq!(outcome.applied_fixes.len(), 1);
        assert_eq!(outcome.applied_fixes[0]["strategy"], "engine_innodb");
        assert_eq!(outcome.applied_fixes[0]["success"], true);
    }

    #[test]
    fn oneclick_apply_plan_executes_charset_sql_in_fk_order_with_rollback_metadata() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "tf_oneclick_charset",
            "steps": [{
                "issue_type": "charset_issue",
                "location": "tf_oneclick_charset.tf_oneclick_parent",
                "table_name": "tf_oneclick_parent",
                "selected_option": {
                    "strategy": "charset_collation_fk_safe",
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                    ],
                    "rollback_sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                    ]
                }
            }]
        }));
        let mut adapter = RecordingAdapter::default();

        let outcome = oneclick_execute_apply_plan(&plan, &mut adapter);

        assert_eq!(
            adapter.executed_sql,
            vec![
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            ]
        );
        assert_eq!(outcome.success_count, 1);
        assert_eq!(outcome.fail_count, 0);
        assert_eq!(outcome.applied_fixes[0]["issue_type"], "charset_issue");
        assert_eq!(
            outcome.applied_fixes[0]["strategy"],
            "charset_collation_fk_safe"
        );
        assert_eq!(
            outcome.applied_fixes[0]["tables"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            outcome.applied_fixes[0]["fk_order"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(outcome.applied_fixes[0]["target_charset"], "utf8mb4");
        assert_eq!(
            outcome.applied_fixes[0]["target_collation"],
            "utf8mb4_0900_ai_ci"
        );
        assert_eq!(
            outcome.applied_fixes[0]["rollback_sql"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(outcome.applied_fixes[0]["success"], true);
    }

    #[test]
    fn oneclick_apply_plan_reports_charset_failure_with_rollback_metadata() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "tf_oneclick_charset",
            "steps": [{
                "issue_type": "charset_issue",
                "location": "tf_oneclick_charset.tf_oneclick_parent",
                "table_name": "tf_oneclick_parent",
                "selected_option": {
                    "strategy": "charset_collation_fk_safe",
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                    ],
                    "rollback_sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                    ]
                }
            }]
        }));
        let mut adapter = RecordingAdapter {
            fail_sql_contains: Some("tf_oneclick_child".to_string()),
            ..RecordingAdapter::default()
        };

        let outcome = oneclick_execute_apply_plan(&plan, &mut adapter);

        assert_eq!(
            adapter.executed_sql,
            vec!["ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"]
        );
        assert_eq!(outcome.success_count, 0);
        assert_eq!(outcome.fail_count, 1);
        assert!(outcome
            .log
            .iter()
            .any(|line| line.contains("tf_oneclick_child") && line.contains("FAILED")));
        assert_eq!(outcome.applied_fixes[0]["success"], false);
        assert!(outcome.applied_fixes[0]["error"]
            .as_str()
            .unwrap()
            .contains("SQL execution error"));
        assert_eq!(
            outcome.applied_fixes[0]["rollback_sql"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }

    #[test]
    fn oneclick_streaming_emit_failure_stops_before_followup_or_side_effect() {
        let request = Request {
            command: "oneclick.run".to_string(),
            request_id: Some("oneclick-emit-failure".to_string()),
            payload: json!({"dry_run": true}),
        };
        let mut calls = 0;

        let error = oneclick_run_streaming(&request, |_event| {
            calls += 1;
            Err(ProtocolEmitError::io("broken oneclick emitter"))
        })
        .expect_err("oneclick emitter failure must propagate");

        assert_eq!(calls, 1);
        assert!(!error.side_effect_started());
    }

    fn oneclick_plan_column(
        ordinal_position: u32,
        name: &str,
        column_type: &str,
    ) -> ActionColumnFact {
        ActionColumnFact {
            ordinal_position,
            name: name.to_string(),
            column_type: column_type.to_string(),
            nullable: false,
            default: ColumnDefaultFact::Absent,
            charset: None,
            collation: None,
            generated_expression: None,
            generated_stored: None,
        }
    }

    fn oneclick_plan_index(name: &str, column: &str) -> ActionIndexFact {
        ActionIndexFact {
            name: name.to_string(),
            unique: name == "PRIMARY",
            index_type: "BTREE".to_string(),
            visible: true,
            columns: vec![ActionIndexColumnFact {
                ordinal_position: 1,
                column_name: Some(column.to_string()),
                expression: None,
                prefix_length: None,
            }],
        }
    }

    fn oneclick_plan_table(
        table: &str,
        engine: &str,
        charset: &str,
        collation: &str,
        columns: Vec<ActionColumnFact>,
        indexes: Vec<ActionIndexFact>,
    ) -> ActionTableDefinitionFact {
        ActionTableDefinitionFact {
            schema: "app".to_string(),
            table: table.to_string(),
            engine: Some(engine.to_string()),
            charset: Some(charset.to_string()),
            collation: Some(collation.to_string()),
            columns,
            indexes,
        }
    }

    fn oneclick_plan_engine_facts() -> ActionFactsDocument {
        ActionFactsDocument {
            action_facts_version: ACTION_FACTS_VERSION,
            action_type: OneClickActionType::EngineInnodb,
            tables: vec![oneclick_plan_table(
                "legacy",
                "MyISAM",
                "utf8mb3",
                "utf8mb3_general_ci",
                vec![oneclick_plan_column(1, "id", "int")],
                vec![oneclick_plan_index("PRIMARY", "id")],
            )],
            foreign_keys: vec![],
        }
    }

    fn oneclick_plan_charset_facts() -> ActionFactsDocument {
        ActionFactsDocument {
            action_facts_version: ACTION_FACTS_VERSION,
            action_type: OneClickActionType::CharsetFkSafe,
            tables: vec![
                oneclick_plan_table(
                    "parent",
                    "InnoDB",
                    "utf8mb3",
                    "utf8mb3_general_ci",
                    vec![oneclick_plan_column(1, "id", "int")],
                    vec![oneclick_plan_index("PRIMARY", "id")],
                ),
                oneclick_plan_table(
                    "child",
                    "InnoDB",
                    "utf8mb3",
                    "utf8mb3_general_ci",
                    vec![
                        oneclick_plan_column(2, "parent_id", "int"),
                        oneclick_plan_column(1, "id", "int"),
                    ],
                    vec![
                        oneclick_plan_index("fk_child_parent", "parent_id"),
                        oneclick_plan_index("PRIMARY", "id"),
                    ],
                ),
            ],
            foreign_keys: vec![ActionForeignKeyFact {
                constraint_schema: "app".to_string(),
                constraint_name: "fk_child_parent".to_string(),
                table_schema: "app".to_string(),
                table_name: "child".to_string(),
                referenced_table_schema: "app".to_string(),
                referenced_table_name: "parent".to_string(),
                match_option: "NONE".to_string(),
                update_rule: "RESTRICT".to_string(),
                delete_rule: "CASCADE".to_string(),
                columns: vec![ActionForeignKeyColumnFact {
                    ordinal_position: 1,
                    column_name: "parent_id".to_string(),
                    referenced_column_name: "id".to_string(),
                }],
            }],
        }
    }

    #[test]
    fn oneclick_plan_default_wire_forms_are_exact_and_strict() {
        let cases = [
            (ColumnDefaultFact::Absent, json!("absent")),
            (ColumnDefaultFact::Null, json!("null")),
            (
                ColumnDefaultFact::Literal("0".to_string()),
                json!({"literal": "0"}),
            ),
            (
                ColumnDefaultFact::Expression("CURRENT_TIMESTAMP".to_string()),
                json!({"expression": "CURRENT_TIMESTAMP"}),
            ),
        ];
        for (fact, wire) in cases {
            assert_eq!(serde_json::to_value(&fact).unwrap(), wire);
            assert_eq!(
                serde_json::from_value::<ColumnDefaultFact>(wire).unwrap(),
                fact
            );
        }

        for rejected in [
            json!("ABSENT"),
            json!("default"),
            json!({"literal": "0", "extra": true}),
            json!({"expression": "NOW()", "literal": "0"}),
            json!({"unknown": "0"}),
        ] {
            assert!(serde_json::from_value::<ColumnDefaultFact>(rejected).is_err());
        }
    }

    #[test]
    fn oneclick_plan_action_fact_golden_vectors_match_exact_bytes_and_hashes() {
        let engine = normalize_action_facts(oneclick_plan_engine_facts()).unwrap();
        assert_eq!(
            canonical_action_facts_json(&engine).unwrap(),
            r#"{"action_facts_version":1,"action_type":"engine_innodb","tables":[{"schema":"app","table":"legacy","engine":"MyISAM","charset":"utf8mb3","collation":"utf8mb3_general_ci","columns":[{"ordinal_position":1,"name":"id","column_type":"int","nullable":false,"default":"absent","charset":null,"collation":null,"generated_expression":null,"generated_stored":null}],"indexes":[{"name":"PRIMARY","unique":true,"index_type":"BTREE","visible":true,"columns":[{"ordinal_position":1,"column_name":"id","expression":null,"prefix_length":null}]}]}],"foreign_keys":[]}"#
        );
        assert_eq!(
            hash_action_facts(&engine).unwrap(),
            "82f25f33ba164c4c2ca938ab3e519561bb881bae6cfa54d6e268b09223c698a5"
        );

        let charset = normalize_action_facts(oneclick_plan_charset_facts()).unwrap();
        assert_eq!(
            hash_action_facts(&charset).unwrap(),
            "ec651d11903da08bbc0092ef468d38d886254e3edb0625cb3105994d91873e20"
        );
        assert_eq!(charset.tables[0].table, "child");
        assert_eq!(charset.tables[0].columns[0].name, "id");
        assert_eq!(charset.tables[0].indexes[0].name, "PRIMARY");
    }

    #[test]
    fn oneclick_plan_normalization_rejects_malformed_ordinals_and_fields() {
        let mut duplicate_column = oneclick_plan_engine_facts();
        duplicate_column.tables[0]
            .columns
            .push(oneclick_plan_column(1, "other", "int"));
        assert_eq!(
            normalize_action_facts(duplicate_column).unwrap_err().code(),
            "oneclick_plan_noncanonical"
        );

        let mut generated = oneclick_plan_engine_facts();
        generated.tables[0].columns[0].generated_stored = Some(true);
        assert_eq!(
            normalize_action_facts(generated).unwrap_err().code(),
            "oneclick_plan_invalid_facts"
        );

        let mut index = oneclick_plan_engine_facts();
        index.tables[0].indexes[0].columns[0].expression = Some("id".to_string());
        assert_eq!(
            normalize_action_facts(index).unwrap_err().code(),
            "oneclick_plan_invalid_facts"
        );

        let mut missing_index_column = oneclick_plan_engine_facts();
        missing_index_column.tables[0].indexes[0].columns[0].column_name =
            Some("missing".to_string());
        assert_eq!(
            normalize_action_facts(missing_index_column)
                .unwrap_err()
                .code(),
            "oneclick_plan_invalid_facts"
        );

        let mut fk = oneclick_plan_charset_facts();
        fk.foreign_keys[0].columns[0].ordinal_position = 2;
        assert_eq!(
            normalize_action_facts(fk).unwrap_err().code(),
            "oneclick_plan_noncanonical"
        );

        let mut missing_fk_column = oneclick_plan_charset_facts();
        missing_fk_column.foreign_keys[0].columns[0].referenced_column_name =
            "missing".to_string();
        assert_eq!(
            normalize_action_facts(missing_fk_column)
                .unwrap_err()
                .code(),
            "oneclick_plan_invalid_facts"
        );
    }

    #[test]
    fn oneclick_plan_normalization_rejects_empty_index_and_fk_members() {
        let mut empty_index = oneclick_plan_engine_facts();
        empty_index.tables[0].indexes[0].columns.clear();
        assert_eq!(
            normalize_action_facts(empty_index).unwrap_err().code(),
            "oneclick_plan_invalid_facts"
        );

        let mut empty_fk = oneclick_plan_charset_facts();
        empty_fk.foreign_keys[0].columns.clear();
        assert_eq!(
            normalize_action_facts(empty_fk).unwrap_err().code(),
            "oneclick_plan_invalid_facts"
        );

        let mut cross_schema_fk = oneclick_plan_charset_facts();
        cross_schema_fk.foreign_keys[0].constraint_schema = "other".to_string();
        assert_eq!(
            normalize_action_facts(cross_schema_fk)
                .unwrap_err()
                .code(),
            "oneclick_plan_invalid_facts"
        );
    }

    #[test]
    fn oneclick_plan_schema_and_payload_parser_are_strict() {
        assert_eq!(normalize_oneclick_schema("App_One").unwrap(), "App_One");
        for invalid in ["", " app", "app ", "app\0prod"] {
            assert_eq!(
                normalize_oneclick_schema(invalid).unwrap_err().code(),
                "oneclick_schema_invalid"
            );
        }

        let request = |payload| Request {
            command: "oneclick.plan".to_string(),
            request_id: Some("plan-parser-1".to_string()),
            payload,
        };
        let accepted = parse_oneclick_plan_request(&request(json!({
            "connection": {
                "engine": "mysql", "host": "127.0.0.1", "port": 3306,
                "user": "app", "password": "secret"
            },
            "schema": "App_One"
        })))
        .unwrap();
        assert_eq!(accepted.schema, "App_One");
        assert_eq!(accepted.endpoint.database, "App_One");
        assert_eq!(accepted.endpoint.schema.as_deref(), Some("App_One"));
        let equal = parse_oneclick_plan_request(&request(json!({
            "connection": {
                "engine": "mysql", "host": "127.0.0.1", "port": 3306,
                "user": "app", "password": "secret",
                "database": "App_One", "schema": "App_One"
            },
            "schema": "App_One"
        })))
        .unwrap();
        assert_eq!(equal.endpoint.database, "App_One");
        assert_eq!(equal.endpoint.schema.as_deref(), Some("App_One"));

        for key in ["database", "schema"] {
            let mut connection = json!({
                "engine": "mysql", "host": "127.0.0.1", "port": 3306,
                "user": "app", "password": "secret"
            });
            connection[key] = json!("other");
            assert_eq!(
                parse_oneclick_plan_request(&request(json!({
                    "connection": connection, "schema": "App_One"
                })))
                .unwrap_err()
                .code(),
                "oneclick_schema_mismatch"
            );

            for invalid in [json!(null), json!(42), json!(" App_One"), json!("App_One ")] {
                let mut connection = json!({
                    "engine": "mysql", "host": "127.0.0.1", "port": 3306,
                    "user": "app", "password": "secret"
                });
                connection[key] = invalid;
                assert_eq!(
                    parse_oneclick_plan_request(&request(json!({
                        "connection": connection, "schema": "App_One"
                    })))
                    .unwrap_err()
                    .code(),
                    "oneclick_schema_mismatch"
                );
            }
        }

        let prohibited = [
            "issues",
            "charset_contracts",
            "target_charset",
            "target_collation",
            "actions",
            "steps",
            "profile",
            "remediation_profile",
            "approval",
            "dry_run",
            "backup_confirmed",
            "unknown",
        ];
        for key in prohibited {
            let mut root = json!({
                "connection": {
                    "engine": "mysql", "host": "127.0.0.1", "port": 3306,
                    "user": "app", "password": "secret"
                },
                "schema": "app"
            });
            root[key] = json!(true);
            assert_eq!(
                parse_oneclick_plan_request(&request(root))
                    .unwrap_err()
                    .code(),
                "oneclick_plan_payload_prohibited"
            );

            let mut nested = json!({
                "engine": "mysql", "host": "127.0.0.1", "port": 3306,
                "user": "app", "password": "secret"
            });
            nested[key] = json!(true);
            assert_eq!(
                parse_oneclick_plan_request(&request(json!({
                    "connection": nested, "schema": "app"
                })))
                .unwrap_err()
                .code(),
                "oneclick_plan_payload_prohibited"
            );
        }
    }

    #[derive(Clone)]
    struct RecordingPlanningSession {
        profile_supported: bool,
        calls: Vec<&'static str>,
        inspection: InspectionResult,
        tables: Vec<ActionTableDefinitionFact>,
        foreign_keys: Vec<ActionForeignKeyFact>,
    }

    impl OneClickPlanningSession for RecordingPlanningSession {
        fn profile_supported(
            &mut self,
            _profile: &OneClickRemediationProfile,
        ) -> Result<bool, String> {
            self.calls.push("profile");
            Ok(self.profile_supported)
        }

        fn read_target_identity(
            &mut self,
            endpoint: &Endpoint,
        ) -> Result<OneClickTargetIdentity, String> {
            self.calls.push("identity");
            Ok(OneClickTargetIdentity {
                engine: endpoint.engine.clone(),
                route: OneClickRoute {
                    host: endpoint.host.clone(),
                    port: endpoint.port,
                },
                server_uuid: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee".to_string(),
                authenticated_user: "app@localhost".to_string(),
                schema: endpoint.database.clone(),
            })
        }

        fn inspect(&mut self, _endpoint: &Endpoint) -> Result<InspectionResult, String> {
            self.calls.push("inspect");
            Ok(self.inspection.clone())
        }

        fn read_table_definitions(
            &mut self,
            _schema: &str,
        ) -> Result<Vec<ActionTableDefinitionFact>, String> {
            self.calls.push("tables");
            Ok(self.tables.clone())
        }

        fn read_fk_facts(
            &mut self,
            _schema: &str,
        ) -> Result<Vec<ActionForeignKeyFact>, String> {
            self.calls.push("fks");
            Ok(self.foreign_keys.clone())
        }
    }

    fn oneclick_plan_recording_session(profile_supported: bool) -> RecordingPlanningSession {
        let engine = oneclick_plan_engine_facts();
        let charset = oneclick_plan_charset_facts();
        let mut legacy = engine.tables[0].clone();
        legacy.charset = Some("utf8mb4".to_string());
        legacy.collation = Some("utf8mb4_0900_ai_ci".to_string());
        RecordingPlanningSession {
            profile_supported,
            calls: Vec::new(),
            inspection: InspectionResult {
                unsupported_objects: vec!["deprecated_engine:legacy:MyISAM".to_string()],
                ..InspectionResult::default()
            },
            tables: vec![legacy, charset.tables[1].clone(), charset.tables[0].clone()],
            foreign_keys: charset.foreign_keys,
        }
    }

    fn oneclick_plan_endpoint() -> Endpoint {
        Endpoint {
            engine: "mysql".to_string(),
            host: "127.0.0.1".to_string(),
            port: 3306,
            user: "app".to_string(),
            password: "do-not-serialize".to_string(),
            database: "app".to_string(),
            schema: Some("app".to_string()),
        }
    }

    fn rehash_action(action: &mut OneClickApplyAction) {
        for expectation in [
            &mut action.expected_pre_facts,
            &mut action.expected_post_facts,
        ] {
            expectation.facts = normalize_action_facts(expectation.facts.clone()).unwrap();
            expectation.facts_hash = hash_action_facts(&expectation.facts).unwrap();
        }
    }

    fn rehash_plan(plan: &mut OneClickPlanEnvelope) {
        for action in &mut plan.actions {
            rehash_action(action);
        }
        plan.snapshot_hash = hash_snapshot(&plan.snapshot).unwrap();
        plan.plan_hash = compute_oneclick_plan_hash(plan).unwrap();
    }

    struct RecordingOneClickSession {
        profile_supported: bool,
        identity: OneClickTargetIdentity,
        inspection: InspectionResult,
        tables: Vec<ActionTableDefinitionFact>,
        foreign_keys: Vec<ActionForeignKeyFact>,
        lock_result: Result<bool, String>,
        release_error: bool,
        sql_error: bool,
        fact_reads: VecDeque<Result<ActionFactsDocument, String>>,
        calls: Vec<String>,
        executed_sql: Vec<String>,
    }

    impl RecordingOneClickSession {
        fn from_planning(planning: &RecordingPlanningSession, endpoint: &Endpoint) -> Self {
            let mut identity_session = planning.clone();
            let identity = identity_session.read_target_identity(endpoint).unwrap();
            Self {
                profile_supported: planning.profile_supported,
                identity,
                inspection: planning.inspection.clone(),
                tables: planning.tables.clone(),
                foreign_keys: planning.foreign_keys.clone(),
                lock_result: Ok(true),
                release_error: false,
                sql_error: false,
                fact_reads: VecDeque::new(),
                calls: Vec::new(),
                executed_sql: Vec::new(),
            }
        }

        fn queue_successful_action_facts(&mut self, plan: &OneClickPlanEnvelope) {
            for action in &plan.actions {
                self.fact_reads
                    .push_back(Ok(action.expected_pre_facts.facts.clone()));
                self.fact_reads
                    .push_back(Ok(action.expected_post_facts.facts.clone()));
            }
        }
    }

    impl OneClickPlanningSession for RecordingOneClickSession {
        fn profile_supported(
            &mut self,
            _profile: &OneClickRemediationProfile,
        ) -> Result<bool, String> {
            self.calls.push("profile".to_string());
            Ok(self.profile_supported)
        }

        fn read_target_identity(
            &mut self,
            _endpoint: &Endpoint,
        ) -> Result<OneClickTargetIdentity, String> {
            self.calls.push("identity".to_string());
            Ok(self.identity.clone())
        }

        fn inspect(&mut self, _endpoint: &Endpoint) -> Result<InspectionResult, String> {
            self.calls.push("inspect".to_string());
            Ok(self.inspection.clone())
        }

        fn read_table_definitions(
            &mut self,
            _schema: &str,
        ) -> Result<Vec<ActionTableDefinitionFact>, String> {
            self.calls.push("tables".to_string());
            Ok(self.tables.clone())
        }

        fn read_fk_facts(
            &mut self,
            _schema: &str,
        ) -> Result<Vec<ActionForeignKeyFact>, String> {
            self.calls.push("fks".to_string());
            Ok(self.foreign_keys.clone())
        }
    }

    impl OneClickApplySession for RecordingOneClickSession {
        fn acquire_advisory_lock(&mut self, key: &str, seconds: u32) -> Result<bool, String> {
            self.calls.push(format!("lock:{key}:{seconds}"));
            self.lock_result.clone()
        }

        fn release_advisory_lock(&mut self, key: &str) -> Result<(), String> {
            self.calls.push(format!("release:{key}"));
            if self.release_error {
                Err("simulated release failure".to_string())
            } else {
                Ok(())
            }
        }

        fn read_action_facts(
            &mut self,
            action: &OneClickApplyAction,
        ) -> Result<ActionFactsDocument, String> {
            self.calls.push(format!("facts:{}", action.ordinal));
            self.fact_reads
                .pop_front()
                .unwrap_or_else(|| Err("missing recorded facts".to_string()))
        }

        fn execute_sql(&mut self, sql: &str) -> Result<(), String> {
            self.calls.push(format!("sql:{sql}"));
            self.executed_sql.push(sql.to_string());
            if self.sql_error {
                Err("simulated SQL failure".to_string())
            } else {
                Ok(())
            }
        }
    }

    fn oneclick_two_action_fixture() -> (
        Endpoint,
        RecordingPlanningSession,
        OneClickPlanEnvelope,
        ValidatedOneClickApplyRequest,
    ) {
        let endpoint = oneclick_plan_endpoint();
        let mut planning = oneclick_plan_recording_session(true);
        planning.foreign_keys.clear();
        planning.tables.truncate(2);
        let plan = build_oneclick_plan(&mut planning, &endpoint, "app").unwrap();
        assert_eq!(plan.actions.len(), 2);
        let validated = ValidatedOneClickApplyRequest {
            endpoint: endpoint.clone(),
            schema: "app".to_string(),
            approval: oneclick_approval_artifact(&plan),
        };
        (endpoint, planning, plan, validated)
    }

    fn assert_precondition_stale(mutator: fn(&mut ActionFactsDocument)) {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();
        let mut session = RecordingOneClickSession::from_planning(&planning, &endpoint);
        let mut stale = plan.actions[0].expected_pre_facts.facts.clone();
        mutator(&mut stale);
        session.fact_reads.push_back(Ok(stale));

        let error = execute_approved_oneclick(&mut session, &validated).unwrap_err();

        assert_eq!(error.code(), "oneclick_precondition_changed");
        assert!(error.applied_ordinals().is_empty());
        assert!(session.executed_sql.is_empty());
        assert_eq!(
            session.calls.iter().filter(|call| call.starts_with("release:")).count(),
            1
        );
    }

    #[test]
    fn oneclick_lock_key_is_uuid_only_base64url_and_deterministic() {
        let (_, planning, _, _) = oneclick_two_action_fixture();
        let endpoint = oneclick_plan_endpoint();
        let mut identity_session = planning.clone();
        let identity = identity_session.read_target_identity(&endpoint).unwrap();
        let key = oneclick_advisory_lock_key(&identity).unwrap();

        assert_eq!(key.len(), 47);
        assert_eq!(key, "tf1:yUaNKbxZldyM5ikF2MREUQdJDuPDGq7d6c8h6uutluw");
        assert!(key.starts_with("tf1:"));
        assert!(key[4..]
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_'));
        assert!(!key.contains('='));
        assert_eq!(oneclick_advisory_lock_key(&identity).unwrap(), key);

        let mut aliases = identity.clone();
        aliases.route.host = "db.internal".to_string();
        aliases.route.port = 4406;
        aliases.authenticated_user = "other@%".to_string();
        aliases.schema = "other".to_string();
        aliases.engine = "alias".to_string();
        assert_eq!(oneclick_advisory_lock_key(&aliases).unwrap(), key);

        let mut upper = aliases.clone();
        upper.server_uuid = identity.server_uuid.to_ascii_uppercase();
        assert_eq!(oneclick_advisory_lock_key(&upper).unwrap(), key);

        let mut other = identity;
        other.server_uuid = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee".to_string();
        assert_ne!(oneclick_advisory_lock_key(&other).unwrap(), key);
    }

    #[test]
    fn oneclick_apply_executor_uses_one_session_and_exact_pre_sql_post_order() {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();
        let mut session = RecordingOneClickSession::from_planning(&planning, &endpoint);
        session.queue_successful_action_facts(&plan);

        let outcome = execute_approved_oneclick(&mut session, &validated).unwrap();

        assert_eq!(outcome.applied_ordinals, vec![1, 2]);
        assert_eq!(
            session.executed_sql,
            plan.actions
                .iter()
                .map(|action| action.sql.clone())
                .collect::<Vec<_>>()
        );
        let facts_and_sql = session
            .calls
            .iter()
            .filter(|call| call.starts_with("facts:") || call.starts_with("sql:"))
            .cloned()
            .collect::<Vec<_>>();
        assert_eq!(
            facts_and_sql,
            vec![
                "facts:1".to_string(),
                format!("sql:{}", plan.actions[0].sql),
                "facts:1".to_string(),
                "facts:2".to_string(),
                format!("sql:{}", plan.actions[1].sql),
                "facts:2".to_string(),
            ]
        );
        assert_eq!(session.calls[0], "identity");
        assert!(session.calls[1].starts_with("lock:tf1:"));
        assert!(session.calls.last().unwrap().starts_with("release:tf1:"));
    }

    #[test]
    fn oneclick_apply_executor_detects_column_generated_index_and_fk_precondition_staleness() {
        assert_precondition_stale(|facts| {
            facts.tables[0].columns[0].column_type = "bigint".to_string();
        });
        assert_precondition_stale(|facts| {
            facts.tables[0].columns[0].generated_expression = Some("id + 1".to_string());
            facts.tables[0].columns[0].generated_stored = Some(true);
            facts.tables[0].columns[0].default = ColumnDefaultFact::Absent;
        });
        assert_precondition_stale(|facts| {
            facts.tables[0].indexes[0].visible = false;
        });
        assert_precondition_stale(|facts| {
            let table = &facts.tables[0];
            facts.foreign_keys.push(ActionForeignKeyFact {
                constraint_schema: table.schema.clone(),
                constraint_name: "fk_stale".to_string(),
                table_schema: table.schema.clone(),
                table_name: table.table.clone(),
                referenced_table_schema: table.schema.clone(),
                referenced_table_name: table.table.clone(),
                match_option: "NONE".to_string(),
                update_rule: "RESTRICT".to_string(),
                delete_rule: "RESTRICT".to_string(),
                columns: vec![ActionForeignKeyColumnFact {
                    ordinal_position: 1,
                    column_name: table.columns[0].name.clone(),
                    referenced_column_name: table.columns[0].name.clone(),
                }],
            });
        });
    }

    #[test]
    fn oneclick_apply_executor_stops_between_actions_and_reports_partial_ordinals() {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();
        let mut session = RecordingOneClickSession::from_planning(&planning, &endpoint);
        session
            .fact_reads
            .push_back(Ok(plan.actions[0].expected_pre_facts.facts.clone()));
        session
            .fact_reads
            .push_back(Ok(plan.actions[0].expected_post_facts.facts.clone()));
        let mut stale = plan.actions[1].expected_pre_facts.facts.clone();
        stale.tables[0].columns[0].column_type = "bigint".to_string();
        session.fact_reads.push_back(Ok(stale));

        let error = execute_approved_oneclick(&mut session, &validated).unwrap_err();

        assert_eq!(error.code(), "oneclick_precondition_changed");
        assert_eq!(error.applied_ordinals(), &[1]);
        assert_eq!(session.executed_sql, vec![plan.actions[0].sql.clone()]);
        assert!(session.calls.last().unwrap().starts_with("release:tf1:"));
    }

    #[test]
    fn oneclick_apply_executor_stops_after_postcondition_drift() {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();
        let mut session = RecordingOneClickSession::from_planning(&planning, &endpoint);
        session
            .fact_reads
            .push_back(Ok(plan.actions[0].expected_pre_facts.facts.clone()));
        let mut stale = plan.actions[0].expected_post_facts.facts.clone();
        stale.tables[0].engine = Some("MyISAM".to_string());
        session.fact_reads.push_back(Ok(stale));

        let error = execute_approved_oneclick(&mut session, &validated).unwrap_err();

        assert_eq!(error.code(), "oneclick_postcondition_changed");
        assert_eq!(error.applied_ordinals(), &[1]);
        assert_eq!(session.executed_sql, vec![plan.actions[0].sql.clone()]);
        assert!(session.calls.last().unwrap().starts_with("release:tf1:"));
    }

    #[test]
    fn oneclick_apply_executor_rejects_snapshot_plan_and_zero_action_replans() {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();

        let mut snapshot_changed = RecordingOneClickSession::from_planning(&planning, &endpoint);
        snapshot_changed.tables[0].columns[0].column_type = "bigint".to_string();
        let error = execute_approved_oneclick(&mut snapshot_changed, &validated).unwrap_err();
        assert_eq!(error.code(), "oneclick_snapshot_changed");
        assert!(snapshot_changed.executed_sql.is_empty());
        assert!(snapshot_changed.calls.last().unwrap().starts_with("release:tf1:"));

        let mut plan_changed = RecordingOneClickSession::from_planning(&planning, &endpoint);
        let mut substituted_plan = validated.clone();
        substituted_plan.approval.plan_hash = "c".repeat(64);
        let error = execute_approved_oneclick(&mut plan_changed, &substituted_plan).unwrap_err();
        assert_eq!(error.code(), "oneclick_plan_changed");
        assert!(plan_changed.executed_sql.is_empty());

        let mut nothing = RecordingOneClickSession::from_planning(&planning, &endpoint);
        nothing.inspection.unsupported_objects.clear();
        for table in &mut nothing.tables {
            table.engine = Some("InnoDB".to_string());
            table.charset = Some("utf8mb4".to_string());
            table.collation = Some("utf8mb4_0900_ai_ci".to_string());
            for column in &mut table.columns {
                if column.charset.is_some() {
                    column.charset = Some("utf8mb4".to_string());
                    column.collation = Some("utf8mb4_0900_ai_ci".to_string());
                }
            }
        }
        let error = execute_approved_oneclick(&mut nothing, &validated).unwrap_err();
        assert_eq!(error.code(), "oneclick_nothing_to_apply");
        assert!(nothing.executed_sql.is_empty());

        assert_eq!(plan.actions.len(), 2);
    }

    #[test]
    fn oneclick_apply_executor_detects_column_generated_index_and_fk_replan_staleness() {
        fn assert_snapshot_changed(mutator: fn(&mut RecordingOneClickSession)) {
            let endpoint = oneclick_plan_endpoint();
            let mut planning = oneclick_plan_recording_session(true);
            let plan = build_oneclick_plan(&mut planning, &endpoint, "app").unwrap();
            let validated = ValidatedOneClickApplyRequest {
                endpoint: endpoint.clone(),
                schema: "app".to_string(),
                approval: oneclick_approval_artifact(&plan),
            };
            let mut session = RecordingOneClickSession::from_planning(&planning, &endpoint);
            mutator(&mut session);

            let error = execute_approved_oneclick(&mut session, &validated).unwrap_err();

            assert_eq!(error.code(), "oneclick_snapshot_changed");
            assert!(error.applied_ordinals().is_empty());
            assert!(session.executed_sql.is_empty());
            assert!(session.calls.last().unwrap().starts_with("release:tf1:"));
        }

        assert_snapshot_changed(|session| {
            session.tables[0].columns[0].column_type = "bigint".to_string();
        });
        assert_snapshot_changed(|session| {
            session.tables[0].columns[0].generated_expression = Some("id + 1".to_string());
            session.tables[0].columns[0].generated_stored = Some(true);
            session.tables[0].columns[0].default = ColumnDefaultFact::Absent;
        });
        assert_snapshot_changed(|session| {
            session.tables[0].indexes[0].visible = false;
        });
        assert_snapshot_changed(|session| {
            session.foreign_keys[0].update_rule = "CASCADE".to_string();
        });
    }

    #[test]
    fn oneclick_apply_executor_handles_lock_target_profile_replan_and_sql_failures() {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();

        let mut lock_unavailable = RecordingOneClickSession::from_planning(&planning, &endpoint);
        lock_unavailable.lock_result = Ok(false);
        let error = execute_approved_oneclick(&mut lock_unavailable, &validated).unwrap_err();
        assert_eq!(error.code(), "oneclick_lock_unavailable");
        assert!(!lock_unavailable
            .calls
            .iter()
            .any(|call| call.starts_with("release:")));

        let mut target_changed = RecordingOneClickSession::from_planning(&planning, &endpoint);
        target_changed.identity.authenticated_user = "other@localhost".to_string();
        let error = execute_approved_oneclick(&mut target_changed, &validated).unwrap_err();
        assert_eq!(error.code(), "oneclick_target_changed");
        assert!(target_changed.calls.last().unwrap().starts_with("release:tf1:"));

        let mut profile_substitution = validated.clone();
        profile_substitution.approval.remediation_profile.profile_id = "substituted".to_string();
        let mut profile_session = RecordingOneClickSession::from_planning(&planning, &endpoint);
        let error = execute_approved_oneclick(&mut profile_session, &profile_substitution)
            .unwrap_err();
        assert_eq!(error.code(), "oneclick_profile_substitution");
        assert!(profile_session.calls.last().unwrap().starts_with("release:tf1:"));

        let mut replan_failed = RecordingOneClickSession::from_planning(&planning, &endpoint);
        replan_failed.tables[0].columns.clear();
        let error = execute_approved_oneclick(&mut replan_failed, &validated).unwrap_err();
        assert_eq!(error.code(), "oneclick_replan_failed");
        assert!(replan_failed.calls.last().unwrap().starts_with("release:tf1:"));

        let mut sql_failed = RecordingOneClickSession::from_planning(&planning, &endpoint);
        sql_failed
            .fact_reads
            .push_back(Ok(plan.actions[0].expected_pre_facts.facts.clone()));
        sql_failed.sql_error = true;
        let error = execute_approved_oneclick(&mut sql_failed, &validated).unwrap_err();
        assert_eq!(error.code(), "oneclick_outcome_indeterminate");
        assert!(error.outcome_indeterminate());
        assert_eq!(error.indeterminate_ordinal(), Some(1));
        assert!(error.applied_ordinals().is_empty());
        assert_eq!(sql_failed.executed_sql, vec![plan.actions[0].sql.clone()]);
        assert!(sql_failed.calls.last().unwrap().starts_with("release:tf1:"));

        let event = oneclick_contract_error_event(
            &Request {
                command: "oneclick.apply_fixes".to_string(),
                request_id: Some("indeterminate-ddl".to_string()),
                payload: json!({}),
            },
            &error,
        );
        assert_eq!(event["code"], "oneclick_outcome_indeterminate");
        assert_eq!(event["outcome_indeterminate"], true);
        assert_eq!(event["indeterminate_ordinal"], 1);
        assert_eq!(event["applied_ordinals"], json!([]));
    }

    #[test]
    fn oneclick_apply_executor_fails_safely_when_successful_lock_release_fails() {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();
        let mut session = RecordingOneClickSession::from_planning(&planning, &endpoint);
        session.queue_successful_action_facts(&plan);
        session.release_error = true;

        let error = execute_approved_oneclick(&mut session, &validated).unwrap_err();

        assert_eq!(error.code(), "oneclick_lock_unavailable");
        assert_eq!(error.applied_ordinals(), &[1, 2]);
        assert!(error.lock_release_failed());
    }

    #[test]
    fn oneclick_apply_executor_preserves_primary_error_when_release_fails() {
        let (endpoint, planning, plan, validated) = oneclick_two_action_fixture();
        let mut session = RecordingOneClickSession::from_planning(&planning, &endpoint);
        let mut stale = plan.actions[0].expected_pre_facts.facts.clone();
        stale.tables[0].columns[0].column_type = "bigint".to_string();
        session.fact_reads.push_back(Ok(stale));
        session.release_error = true;

        let error = execute_approved_oneclick(&mut session, &validated).unwrap_err();

        assert_eq!(error.code(), "oneclick_precondition_changed");
        assert!(error.lock_release_failed());
        assert!(error.applied_ordinals().is_empty());
    }

    #[test]
    fn oneclick_plan_builder_uses_one_session_and_fails_closed_for_fk_charset_tables() {
        let mut session = oneclick_plan_recording_session(true);
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();

        assert_eq!(
            session.calls,
            vec!["profile", "identity", "inspect", "tables", "fks"]
        );
        assert_eq!(plan.plan_version, ONECLICK_PLAN_VERSION);
        assert_eq!(plan.remediation_profile, fixed_oneclick_profile());
        assert_eq!(plan.actions.len(), 1);
        assert_eq!(
            plan.actions.iter().map(|action| action.ordinal).collect::<Vec<_>>(),
            vec![1]
        );
        assert!(plan.actions.iter().all(|action| {
            sql_is_one_statement(&action.sql)
                && action.rollback_sql.as_ref().map_or(true, |sql| {
                    sql_is_one_statement(sql)
                })
        }));
        assert_eq!(plan.actions[0].action_type, OneClickActionType::EngineInnodb);
        assert!(plan
            .actions
            .iter()
            .all(|action| action.action_type != OneClickActionType::CharsetFkSafe));
        assert_eq!(
            plan.snapshot
                .inspection_facts
                .iter()
                .filter(|fact| fact.issue_type == "charset_issue")
                .filter_map(|fact| fact.table.as_deref())
                .collect::<Vec<_>>(),
            vec!["child", "parent"]
        );
        assert_eq!(plan.snapshot.foreign_keys.len(), 1);
        validate_oneclick_plan(&plan).unwrap();

        let wire = serde_json::to_string(&plan).unwrap();
        assert!(!wire.contains("do-not-serialize"));
        assert!(!wire.contains("password"));
        assert!(!wire.contains("connection"));
        assert!(!wire.contains("message"));
    }

    #[test]
    fn oneclick_plan_isolated_charset_table_keeps_one_deterministic_action() {
        let mut session = oneclick_plan_recording_session(true);
        session.tables.push(oneclick_plan_table(
            "standalone",
            "InnoDB",
            "utf8mb3",
            "utf8mb3_general_ci",
            vec![oneclick_plan_column(1, "id", "int")],
            vec![oneclick_plan_index("PRIMARY", "id")],
        ));

        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();
        let charset_actions = plan
            .actions
            .iter()
            .filter(|action| action.action_type == OneClickActionType::CharsetFkSafe)
            .collect::<Vec<_>>();

        assert_eq!(charset_actions.len(), 1);
        assert_eq!(charset_actions[0].tables, vec!["standalone"]);
        assert_eq!(
            charset_actions[0].sql,
            "ALTER TABLE `app`.`standalone` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
        );
        assert!(sql_is_one_statement(&charset_actions[0].sql));
    }

    #[test]
    fn oneclick_plan_deprecated_engine_markers_preserve_and_validate_table_identity() {
        assert_eq!(
            oneclick_deprecated_engine_marker("deprecated_engine:orders:2026:MyISAM"),
            Some(("orders:2026".to_string(), "MyISAM".to_string()))
        );
        assert_eq!(
            oneclick_deprecated_engine_marker("deprecated_engine: orders :MyISAM"),
            Some((" orders ".to_string(), "MyISAM".to_string()))
        );

        let mut session = oneclick_plan_recording_session(true);
        session.tables[0].table = "orders:2026".to_string();
        session.inspection.unsupported_objects =
            vec!["deprecated_engine:orders:2026:MyISAM".to_string()];
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();
        assert_eq!(plan.actions[0].tables, vec!["orders:2026"]);
    }

    #[test]
    fn oneclick_plan_rejects_unknown_mismatched_and_contradictory_engine_markers() {
        let marker_sets = [
            vec!["deprecated_engine:ghost:MyISAM".to_string()],
            vec!["deprecated_engine:legacy:MEMORY".to_string()],
            vec![
                "deprecated_engine:legacy:MyISAM".to_string(),
                "deprecated_engine:legacy:MEMORY".to_string(),
            ],
        ];

        for unsupported_objects in marker_sets {
            let mut session = oneclick_plan_recording_session(true);
            session.inspection.unsupported_objects = unsupported_objects;
            assert_eq!(
                build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app")
                    .unwrap_err()
                    .code(),
                "oneclick_plan_invalid_facts"
            );
        }
    }

    #[test]
    fn oneclick_plan_sql_validation_ignores_only_quoted_semicolons() {
        for sql in [
            "ALTER TABLE `app`.`a;b` ENGINE=InnoDB;",
            "SELECT 'a;''b';",
            "SELECT \"a;\"\"b\";",
            "SELECT `a;``b`;",
            r#"SELECT 'a;\'b';"#,
        ] {
            assert!(sql_is_one_statement(sql), "expected one statement: {sql}");
        }
        for sql in [
            "ALTER TABLE `app`.`a` ENGINE=InnoDB; SELECT 1;",
            "SELECT 1; -- second;",
            "SELECT 'unterminated;",
            "SELECT 1",
        ] {
            assert!(!sql_is_one_statement(sql), "expected rejection: {sql}");
        }

        let mut session = oneclick_plan_recording_session(true);
        session.tables[0].table = "a;b".to_string();
        session.inspection.unsupported_objects =
            vec!["deprecated_engine:a;b:MyISAM".to_string()];
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();
        assert_eq!(plan.actions[0].tables, vec!["a;b"]);
        validate_oneclick_plan(&plan).unwrap();
    }

    #[test]
    fn oneclick_plan_engine_actions_cover_only_deprecated_engines() {
        let mut session = oneclick_plan_recording_session(true);
        let mut memory = session.tables[0].clone();
        memory.table = "scratch".to_string();
        memory.engine = Some("MEMORY".to_string());
        session.tables.push(memory);

        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();
        let engine_tables = plan
            .actions
            .iter()
            .filter(|action| action.action_type == OneClickActionType::EngineInnodb)
            .flat_map(|action| action.tables.iter().map(String::as_str))
            .collect::<Vec<_>>();

        assert_eq!(engine_tables, vec!["legacy"]);
    }

    #[test]
    fn oneclick_plan_unsupported_profile_stops_before_identity_or_plan() {
        let mut session = oneclick_plan_recording_session(false);
        let error = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app")
            .unwrap_err();

        assert_eq!(error.code(), "oneclick_profile_unsupported");
        assert_eq!(session.calls, vec!["profile"]);
    }

    #[test]
    fn oneclick_plan_builder_rejects_non_authoritative_schema_facts() {
        let mut session = oneclick_plan_recording_session(true);
        for table in &mut session.tables {
            table.schema = "other".to_string();
            table.engine = Some("InnoDB".to_string());
            table.charset = Some("utf8mb4".to_string());
            table.collation = Some("utf8mb4_0900_ai_ci".to_string());
        }
        session.foreign_keys.clear();

        assert_eq!(
            build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app")
                .unwrap_err()
                .code(),
            "oneclick_plan_invalid_facts"
        );
    }

    #[test]
    fn oneclick_plan_hashes_bind_typed_state_profile_actions_and_order() {
        let mut session = oneclick_plan_recording_session(true);
        session.tables.push(oneclick_plan_table(
            "standalone",
            "InnoDB",
            "utf8mb3",
            "utf8mb3_general_ci",
            vec![oneclick_plan_column(1, "id", "int")],
            vec![oneclick_plan_index("PRIMARY", "id")],
        ));
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();
        let original = plan.plan_hash.clone();

        let mut changed = plan.clone();
        changed.actions[0].expected_pre_facts.facts.tables[0].columns[0].column_type =
            "bigint".to_string();
        assert_ne!(compute_oneclick_plan_hash(&changed).unwrap(), original);

        let mut changed = plan.clone();
        changed.actions[1].expected_pre_facts.facts.tables[0].columns[0]
            .generated_expression = Some("id + 1".to_string());
        changed.actions[1].expected_pre_facts.facts.tables[0].columns[0].generated_stored =
            Some(true);
        assert_ne!(compute_oneclick_plan_hash(&changed).unwrap(), original);

        let mut changed = plan.clone();
        changed.actions[1].expected_pre_facts.facts.tables[0].indexes[0].visible = false;
        assert_ne!(compute_oneclick_plan_hash(&changed).unwrap(), original);

        let mut changed = plan.clone();
        changed.actions.swap(0, 1);
        assert_ne!(compute_oneclick_plan_hash(&changed).unwrap(), original);

        let mut changed = plan.clone();
        changed.remediation_profile.profile_id = "substituted".to_string();
        assert_ne!(compute_oneclick_plan_hash(&changed).unwrap(), original);

        let mut unknown = serde_json::to_value(oneclick_plan_engine_facts()).unwrap();
        unknown["message"] = json!("not canonical");
        assert!(serde_json::from_value::<ActionFactsDocument>(unknown).is_err());
    }

    #[test]
    fn oneclick_plan_validation_rejects_scope_hash_and_order_substitution() {
        let mut session = oneclick_plan_recording_session(true);
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();

        let approval_artifact = oneclick_approval_artifact(&plan);
        let approval_wire = serde_json::to_string(&approval_artifact).unwrap();
        let declaration_order = [
            "\"approval_version\"",
            "\"plan_version\"",
            "\"target_identity\"",
            "\"remediation_profile\"",
            "\"snapshot_hash\"",
            "\"plan_hash\"",
        ];
        let positions = declaration_order
            .iter()
            .map(|field| approval_wire.find(field).unwrap())
            .collect::<Vec<_>>();
        assert!(positions.windows(2).all(|pair| pair[0] < pair[1]));
        let approval = serde_json::to_value(approval_artifact).unwrap();
        assert_eq!(approval.as_object().unwrap().len(), 6);
        assert!(approval.get("snapshot").is_none());
        assert!(approval.get("actions").is_none());
        assert!(approval.get("facts").is_none());

        let mut changed = plan.clone();
        changed.actions[0].tables = vec!["legacy".to_string(), "legacy".to_string()];
        changed.plan_hash = compute_oneclick_plan_hash(&changed).unwrap();
        assert_eq!(
            validate_oneclick_plan(&changed).unwrap_err().code(),
            "oneclick_plan_noncanonical"
        );

        let mut changed = plan.clone();
        changed.actions[0]
            .expected_pre_facts
            .facts
            .tables
            .push(changed.snapshot.table_definitions[0].clone());
        changed.actions[0].expected_pre_facts.facts_hash = "0".repeat(64);
        changed.plan_hash = compute_oneclick_plan_hash(&changed).unwrap();
        assert!(validate_oneclick_plan(&changed).is_err());

        let mut changed = plan.clone();
        changed.snapshot_hash = "0".repeat(64);
        changed.plan_hash = compute_oneclick_plan_hash(&changed).unwrap();
        assert!(validate_oneclick_plan(&changed).is_err());
    }

    #[test]
    fn oneclick_plan_validation_rejects_fully_rehashed_action_forgery() {
        let mut session = oneclick_plan_recording_session(true);
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();

        let mut empty_facts = plan.clone();
        empty_facts.actions[0].expected_pre_facts.facts.tables.clear();
        empty_facts.actions[0].expected_post_facts.facts.tables.clear();
        empty_facts.actions[0].expected_pre_facts.facts_hash = domain_hash(
            ACTION_FACTS_HASH_DOMAIN,
            &empty_facts.actions[0].expected_pre_facts.facts,
        )
        .unwrap();
        empty_facts.actions[0].expected_post_facts.facts_hash = domain_hash(
            ACTION_FACTS_HASH_DOMAIN,
            &empty_facts.actions[0].expected_post_facts.facts,
        )
        .unwrap();
        empty_facts.plan_hash = compute_oneclick_plan_hash(&empty_facts).unwrap();
        assert!(validate_oneclick_plan(&empty_facts).is_err());

        let mut ghost = plan.clone();
        ghost.actions[0].tables = vec!["ghost".to_string()];
        ghost.actions[0].sql = "ALTER TABLE `app`.`ghost` ENGINE=InnoDB;".to_string();
        ghost.actions[0].rollback_sql =
            Some("ALTER TABLE `app`.`ghost` ENGINE=MyISAM;".to_string());
        ghost.actions[0].expected_pre_facts.facts.tables[0].table = "ghost".to_string();
        ghost.actions[0].expected_post_facts.facts.tables[0].table = "ghost".to_string();
        rehash_plan(&mut ghost);
        assert!(validate_oneclick_plan(&ghost).is_err());

        let mut substituted = plan.clone();
        substituted.actions[0].strategy = "engine_memory".to_string();
        substituted.actions[0].sql = "ALTER TABLE `app`.`legacy` ENGINE=MEMORY;".to_string();
        substituted.actions[0].expected_post_facts.facts.tables[0].engine =
            Some("MEMORY".to_string());
        rehash_plan(&mut substituted);
        assert!(validate_oneclick_plan(&substituted).is_err());
    }

    #[test]
    fn oneclick_plan_validation_rejects_rehashed_order_and_state_chain_forgery() {
        let mut session = oneclick_plan_recording_session(true);
        session.tables[0].charset = Some("utf8mb3".to_string());
        session.tables[0].collation = Some("utf8mb3_general_ci".to_string());
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();

        let mut reordered = plan.clone();
        reordered.actions.swap(0, 1);
        for (position, action) in reordered.actions.iter_mut().enumerate() {
            action.ordinal = (position + 1) as u32;
        }
        rehash_plan(&mut reordered);
        assert!(validate_oneclick_plan(&reordered).is_err());

        let mut stale_pre_state = plan.clone();
        let charset_action = stale_pre_state
            .actions
            .iter_mut()
            .find(|action| {
                action.action_type == OneClickActionType::CharsetFkSafe
                    && action.tables == ["legacy"]
            })
            .unwrap();
        charset_action.expected_pre_facts.facts.tables[0].engine =
            Some("MyISAM".to_string());
        rehash_plan(&mut stale_pre_state);
        assert!(validate_oneclick_plan(&stale_pre_state).is_err());
    }

    #[test]
    fn oneclick_plan_validation_rejects_rehashed_inspection_and_fk_schema_forgery() {
        let mut session = oneclick_plan_recording_session(true);
        let plan = build_oneclick_plan(&mut session, &oneclick_plan_endpoint(), "app").unwrap();

        let mut ghost_inspection = plan.clone();
        ghost_inspection.snapshot.inspection_facts[0].table = Some("ghost".to_string());
        rehash_plan(&mut ghost_inspection);
        assert!(validate_oneclick_plan(&ghost_inspection).is_err());

        let mut foreign_schema = plan.clone();
        foreign_schema.snapshot.foreign_keys[0].constraint_schema = "other".to_string();
        rehash_plan(&mut foreign_schema);
        assert!(validate_oneclick_plan(&foreign_schema).is_err());
    }

    #[test]
    fn oneclick_plan_apply_predicate_requires_both_proofs_and_constants_stay_false() {
        assert!(!ONECLICK_EXACT_PLAN_ENABLED);
        assert!(!ONECLICK_STRONG_FENCE_PROVEN);
        assert!(!oneclick_apply_enabled(false, false));
        assert!(!oneclick_apply_enabled(true, false));
        assert!(!oneclick_apply_enabled(false, true));
        assert!(oneclick_apply_enabled(true, true));
        assert!(!oneclick_apply_enabled(
            ONECLICK_EXACT_PLAN_ENABLED,
            ONECLICK_STRONG_FENCE_PROVEN
        ));
    }
}
