# One-Click Exact-Plan Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans task-by-task. Track every step with its checkbox.

**Goal:** Contain TF-STATUS-097 in two phases: fail closed before TF-STATUS-098, then add canonical plan/approval/recheck contracts without enabling apply until MySQL concurrency can be strongly fenced.

**Architecture:** Rust remains DB owner. Phase A disables every non-dry-run entry while retaining Rust dry-run. After TF-098, Phase B adds plan-only protocol, a fixed Rust remediation profile, exact approval/replan, and a generic candidate executor with bounded advisory locking and immediate action preconditions. MySQL DDL implicitly commits and cannot be fenced from arbitrary external DDL by these defenses, so Rust and PyQt apply gates stay false.

**Tech Stack:** Python 3.9+, PyQt6, Rust/Cargo, MySQL, JSONL v1, serde/serde_json, SHA-256 (`sha2`), base64url (`base64`), pytest.

## Global Constraints
- Rust `tunnelforge-core` alone inspects/mutates DB state; Python only negotiates, renders, approves, and sequences.
- Phase A completes before TF-098. Phase B starts only after its bounded process contract passes the checkpoint below.
- Phase A temporarily preserves current Rust dry-run. In Phase B, `oneclick.plan` is the only preview; legacy `oneclick.run`, `oneclick.recommend`, `oneclick.apply_fixes dry_run=true`, and client-derived charset-contract preview are disabled/deprecated.
- Plans are secret-free and bind identity, fixed remediation profile, snapshot, ordered actions/preconditions, and plan hash.
- Client issues/contracts/actions/SQL/target overrides never select remediation. Errors/logs exclude credentials, approval bodies, and raw snapshots.
- A timed-out/lost mutation is typed `outcome_indeterminate`; no facade, worker, dialog, or evidence tool retries it.
- Phase B apply is enabled only when `ONECLICK_EXACT_PLAN_ENABLED && ONECLICK_STRONG_FENCE_PROVEN`; either false means `oneclick_apply_disabled`. Neither is environment/config overridable.

---

## Phase A: Disable Unsafe Apply Before TF-STATUS-098

### Task 1: RED Mutation Reachability and Fail-Closed Contract
**Files:**
- Modify: `migration_core/src/oneclick.rs` (`#[cfg(test)]` module)
- Modify: `migration_core/src/protocol.rs`
- Modify: `migration_core/tests/live_roundtrip.rs`
- Modify: `tests/test_oneclick_rust_core_gate.py`

**Current mutation path:**
```text
OneClickMigrationDialog.start_migration -> OneClickMigrationWorker.run
  -> DbCoreFacade.run_oneclick -> oneclick.run -> oneclick_run_streaming
  -> oneclick_execute_stage(dry_run=false) -> oneclick_execute_apply_plan
  -> MigrationAdapter::execute_sql
```
`oneclick_apply_plan_executes_engine_innodb_sql` proves SQL reachability; `test_oneclick_worker_allows_limited_real_execution_with_backup_confirmation` proves backup-only UI execution.

- [ ] **Step 1: Add RED direct-entry and UI tests**
```rust
#[test]
fn oneclick_run_non_dry_run_fails_closed_before_endpoint_or_sql() {
    let events = handle_request(Request::test("oneclick.run", json!({"dry_run":false,"backup_confirmed":true})));
    assert_error_code(&events, "oneclick_apply_disabled");
}
#[test]
fn oneclick_apply_fixes_non_dry_run_fails_closed_before_endpoint_or_sql() {
    let events = handle_request(Request::test("oneclick.apply_fixes", json!({"dry_run":false,"backup_confirmed":true})));
    assert_error_code(&events, "oneclick_apply_disabled");
}
```
Update live real-apply tests to expect unchanged MyISAM/charset state. Replace the Python allow test with `test_oneclick_worker_rejects_real_execution_while_plan_approval_is_unavailable`. Assert Dry-run checked/disabled, backup disabled initially and after `_on_finished()` from dry-run, and clear temporary tooltips.

- [ ] **Step 2: Run RED and preserve reachability evidence**
Run: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_plan_executes_engine_innodb_sql --lib`
Expected: PASS, proving the old internal executor can mutate.
Run: `cargo test --manifest-path migration_core\Cargo.toml oneclick_run_non_dry_run_fails_closed_before_endpoint_or_sql --lib`
Run: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_fixes_non_dry_run_fails_closed_before_endpoint_or_sql --lib`
Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_oneclick_rust_core_gate.py -q`
Expected: new safety tests FAIL against the current open gates.

### Task 2: GREEN Minimal Gate, Review, Evidence, and Commit
**Files:**
- Modify: `migration_core/src/oneclick.rs`, `migration_core/src/protocol.rs`, `migration_core/tests/live_roundtrip.rs`
- Modify: `src/ui/dialogs/oneclick_migration_dialog.py`, `src/ui/dialogs/migration_dialogs.py`
- Modify: `src/core/i18n/legacy_translate.py`, `tests/test_i18n.py`, `tests/test_oneclick_rust_core_gate.py`
- Modify after verification: `docs/current_status.md`

- [ ] **Step 1: Gate both Rust entries**
In `oneclick_run_streaming`, reject false `dry_run` before `oneclick_preflight_state`. Temporarily keep current true dry-run in Phase A only. In `oneclick_apply_fixes`, temporarily retain true preview but reject false before client actions, endpoint, adapter, or SQL. Emit `oneclick_apply_disabled`; Phase B removes both preview paths in favor of `oneclick.plan`.

- [ ] **Step 2: Gate worker/UI and control restoration**
Set `ONECLICK_REAL_EXECUTION_ENABLED=False`. `OneClickMigrationWorker.run()` calls `_require_execution_enabled()` before facade access for false dry-run. Add `_sync_execution_controls()` after widget creation and in `_on_finished()`; enable Dry-run only if the flag is true and backup only if the flag is true and Dry-run is unchecked. Use exact Korean copy `정확한 실행 계획 승인 보호가 준비될 때까지 실제 변경은 비활성화됩니다. Dry-run 미리보기는 계속 사용할 수 있습니다.` in both dialogs with exact English mapping/tests.

- [ ] **Step 3: Verify GREEN**
Run: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`
Run: `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip oneclick -- --nocapture`
Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py tests\test_i18n.py -q`
Expected: both direct mutations stop before DB setup; dry-run passes; backup stays disabled after completion.

- [ ] **Step 4: Review/evidence and commit**
Invoke `superpowers:requesting-code-review` for Phase A, resolve High/Critical findings, rerun Step 3 and full Python/Rust tests, then record the temporary gate in all aligned `docs/current_status.md` sections without closing TF-097.
```powershell
git add migration_core/src/oneclick.rs migration_core/src/protocol.rs migration_core/tests/live_roundtrip.rs src/ui/dialogs/oneclick_migration_dialog.py src/ui/dialogs/migration_dialogs.py src/core/i18n/legacy_translate.py tests/test_oneclick_rust_core_gate.py tests/test_i18n.py docs/current_status.md
git commit -m "Fix: disable unsafe One-Click apply"
```

---

## Dependency Checkpoint: TF-STATUS-098 Complete

TF-STATUS-098 landed through commit `420518e`. `DbCoreServiceClient` now uses
bounded request deadlines, strict request-ID/protocol validation, negotiated
process generations, and owned poison/reap barriers. The old unbounded
`stdout.readline()` and mismatched-ID discard behavior is historical RED
evidence, not the current contract.

**Required compatibility API in `src/core/db_core_client.py`:**
```python
class DbCoreRequestKind(Enum): READ_ONLY = "read_only"; MUTATION = "mutation"
class DbCoreOutcome(Enum):
    DEFINITE = "definite"; NOT_STARTED = "not_started"
    FAILED = "failed"; OUTCOME_INDETERMINATE = "outcome_indeterminate"
@dataclass(frozen=True)
class DbCoreRequestResult:
    request_kind: DbCoreRequestKind; outcome: DbCoreOutcome
    request_id: str; process_generation: int; message: str
    rust_code: Optional[str]; payload: Mapping[str, Any]
class DbCoreServiceError(RuntimeError):
    code: str; request_kind: DbCoreRequestKind; outcome: DbCoreOutcome
    request_id: str
    process_generation: int; rust_code: Optional[str]
```
`request_result(..., request_kind=..., on_event=...) -> DbCoreRequestResult` is the new structural API. `request_payload(...) -> Dict[str,Any]` returns `request_result(...).payload`; existing `request(...) -> Dict[str,Any]` remains a compatibility wrapper over `request_payload`, preserving all current facade consumers. New One-Click methods use `request_result`; no mixed `.get()` on `DbCoreRequestResult` is allowed.

Stateful DB consumers use `DbCoreConnectionHandle(connection_id,
process_generation)`, validated against the facade's issued-handle registry;
raw, cloned, foreign, malformed, or stale-generation handles fail before wire
admission.

Every Rust failure migrates to exact structured wire shape `{"event":"error","request_id":"<matching id>","code":"<stable nonempty code>","message":"<safe text>"}`. Python validates event/type/ID/code before raising `DbCoreServiceError`; malformed, missing-code, string-only, or mismatched error lines are `db_core_protocol_mismatch`, never message-parsed Rust failures.

- [x] Land and commit TF-098 implementation; status is `closed` with bounded reads, strict ID/protocol handling, terminate/reap, recovery, and zero mutation resend. Python codes include `db_core_capability_missing`, `db_core_protocol_mismatch`, `db_core_request_id_mismatch`, `db_core_timeout`, and `db_core_process_died`, preserving structured Rust `code` in `rust_code`.
- [x] Require per-generation `service.hello` negotiation of `protocol_version=1`, `process_contract_version=1`, and capabilities `request.deadline`, `request.strict_id`, `process.generation`, `mutation.outcome_indeterminate`. Phase B must classify `oneclick.plan` read-only and `oneclick.apply_fixes` mutation when those commands are introduced.
- [x] Test `request_result`, `request_payload`, and legacy `request` for structured result/error events; reject missing/empty/nonstring code, string-only errors, wrong IDs, old/missing capability, version mismatch, timeout, and death; verify metadata, preserved Rust code, recovery, and no retry.
- [x] Run the compatibility consumer gate and full strict Python gate, covering connection/schema/query, dump/import, migration, connector, and UI consumers; final local evidence is recorded in `docs/current_status.md`.
- [x] Checkpoint satisfied at `420518e`; Task 3 may begin. Phase A mutation gates remain active until the later exact-plan and strong-fence requirements are independently proven.

---

## Phase B: Canonical Plan, Approval, Replan, and Fencing Decision

### Task 3: Canonical Rust Plan, Profile, and Action Facts
**Files:**
- Modify: `migration_core/src/oneclick.rs`, `migration_core/src/schema.rs`, `migration_core/src/protocol.rs`
- Test: Rust unit tests in `migration_core/src/oneclick.rs` and `migration_core/src/protocol.rs`

- [ ] **Step 0: Enforce the TF-098 code gate**
Record the landed TF-098 commit SHA and rerun every checkpoint command. If unavailable/failing, leave this task `blocked` without creating or editing any Phase B file; Phase A is the only permitted One-Click state.

**Core wire types:**
```rust
const ONECLICK_PLAN_VERSION: u32 = 1;
const ONECLICK_APPROVAL_VERSION: u32 = 1;
const ONECLICK_PROFILE_VERSION: u32 = 1;
const ACTION_FACTS_VERSION: u32 = 1;
const ONECLICK_EXACT_PLAN_ENABLED: bool = false;
const ONECLICK_STRONG_FENCE_PROVEN: bool = false;
fn oneclick_apply_enabled(exact_plan_enabled: bool, strong_fence_proven: bool) -> bool {
    exact_plan_enabled && strong_fence_proven
}

struct OneClickRoute { host: String, port: u16 }
struct OneClickTargetIdentity { engine: String, route: OneClickRoute, server_uuid: String,
    authenticated_user: String, schema: String }
struct OneClickRemediationProfile { profile_version: u32, profile_id: String,
    target_charset: String, target_collation: String }
#[serde(rename_all = "snake_case")]
enum OneClickActionType { EngineInnodb, CharsetFkSafe }
enum ColumnDefaultFact { Absent, Null, Literal(String), Expression(String) }
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct LiteralDefaultWire { literal: String }
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExpressionDefaultWire { expression: String }
#[derive(Serialize, Deserialize)]
#[serde(untagged)]
enum ColumnDefaultWire { Keyword(String), Literal(LiteralDefaultWire), Expression(ExpressionDefaultWire) }
struct ActionColumnFact { ordinal_position: u32, name: String, column_type: String,
    nullable: bool, default: ColumnDefaultFact, charset: Option<String>, collation: Option<String>,
    generated_expression: Option<String>, generated_stored: Option<bool> }
struct ActionIndexColumnFact { ordinal_position: u32, column_name: Option<String>,
    expression: Option<String>, prefix_length: Option<u32> }
struct ActionIndexFact { name: String, unique: bool, index_type: String,
    visible: bool, columns: Vec<ActionIndexColumnFact> }
struct ActionTableDefinitionFact { schema: String, table: String, engine: Option<String>,
    charset: Option<String>, collation: Option<String>, columns: Vec<ActionColumnFact>,
    indexes: Vec<ActionIndexFact> }
struct ActionForeignKeyColumnFact { ordinal_position: u32, column_name: String,
    referenced_column_name: String }
struct ActionForeignKeyFact { constraint_schema: String, constraint_name: String,
    table_schema: String, table_name: String,
    referenced_table_schema: String, referenced_table_name: String,
    match_option: String, update_rule: String, delete_rule: String,
    columns: Vec<ActionForeignKeyColumnFact> }
struct ActionFactsDocument { action_facts_version: u32, action_type: OneClickActionType,
    tables: Vec<ActionTableDefinitionFact>, foreign_keys: Vec<ActionForeignKeyFact> }
struct OneClickActionStateExpectation { facts: ActionFactsDocument, facts_hash: String }
struct OneClickApplyAction { ordinal: u32, action_type: OneClickActionType,
    issue_type: String, strategy: String, schema: String, tables: Vec<String>,
    sql: String, rollback_sql: Option<String>, target_charset: Option<String>,
    target_collation: Option<String>, expected_pre_facts: OneClickActionStateExpectation,
    expected_post_facts: OneClickActionStateExpectation }
struct OneClickInspectionFact { issue_type: String, severity: String, object_kind: String,
    schema: String, table: Option<String>, column: Option<String> }
struct OneClickSnapshotDocument { snapshot_version: u32, schema: String,
    inspection_facts: Vec<OneClickInspectionFact>,
    table_definitions: Vec<ActionTableDefinitionFact>, foreign_keys: Vec<ActionForeignKeyFact> }
struct OneClickPlanEnvelope { plan_version: u32, target_identity: OneClickTargetIdentity,
    remediation_profile: OneClickRemediationProfile, snapshot: OneClickSnapshotDocument,
    snapshot_hash: String,
    actions: Vec<OneClickApplyAction>, plan_hash: String }
struct OneClickApprovalArtifact { approval_version: u32, plan_version: u32,
    target_identity: OneClickTargetIdentity, remediation_profile: OneClickRemediationProfile,
    snapshot_hash: String, plan_hash: String }
```
All hash/wire structs derive `Serialize`; accepted inputs derive `Deserialize` with unknown fields denied. `ColumnDefaultFact` has custom `Serialize`/`Deserialize` via `ColumnDefaultWire`: `Absent -> "absent"`, `Null -> "null"`, `Literal(v) -> {"literal":v}`, `Expression(v) -> {"expression":v}`; reject every other keyword/object/extra key. Thus golden `"default":"absent"` bytes are exact. Declaration order is canonical. Profile v1 is fixed/DB-validated: `mysql-utf8mb4-0900-v1`, `utf8mb4`, `utf8mb4_0900_ai_ci`; unsupported returns `oneclick_profile_unsupported` before plan/SQL.

Normalization uses DB metadata, typed fields, and unsigned lexicographic UTF-8 bytes without locale/case folding. Inspection facts sort by `(issue_type,severity,object_kind,schema,table_or_empty,column_or_empty)`; tables by `(schema,table)`; indexes by exact name bytes (`PRIMARY` before lowercase `fk_child_parent`), then member ordinal; top-level foreign keys by `(constraint_schema,constraint_name,table_schema,table_name,referenced_table_schema,referenced_table_name)`; columns/FK members by ordinal. Reject duplicate/noncontiguous ordinals and malformed index/generated/FK fields. Compact serde plus domain `tunnelforge.oneclick.action-facts.v1\0` yields lowercase SHA-256. Golden vectors:
```text
{"action_facts_version":1,"action_type":"engine_innodb","tables":[{"schema":"app","table":"legacy","engine":"MyISAM","charset":"utf8mb3","collation":"utf8mb3_general_ci","columns":[{"ordinal_position":1,"name":"id","column_type":"int","nullable":false,"default":"absent","charset":null,"collation":null,"generated_expression":null,"generated_stored":null}],"indexes":[{"name":"PRIMARY","unique":true,"index_type":"BTREE","visible":true,"columns":[{"ordinal_position":1,"column_name":"id","expression":null,"prefix_length":null}]}]}],"foreign_keys":[]}
sha256:82f25f33ba164c4c2ca938ab3e519561bb881bae6cfa54d6e268b09223c698a5
{"action_facts_version":1,"action_type":"charset_fk_safe","tables":[{"schema":"app","table":"child","engine":"InnoDB","charset":"utf8mb3","collation":"utf8mb3_general_ci","columns":[{"ordinal_position":1,"name":"id","column_type":"int","nullable":false,"default":"absent","charset":null,"collation":null,"generated_expression":null,"generated_stored":null},{"ordinal_position":2,"name":"parent_id","column_type":"int","nullable":false,"default":"absent","charset":null,"collation":null,"generated_expression":null,"generated_stored":null}],"indexes":[{"name":"PRIMARY","unique":true,"index_type":"BTREE","visible":true,"columns":[{"ordinal_position":1,"column_name":"id","expression":null,"prefix_length":null}]},{"name":"fk_child_parent","unique":false,"index_type":"BTREE","visible":true,"columns":[{"ordinal_position":1,"column_name":"parent_id","expression":null,"prefix_length":null}]}]},{"schema":"app","table":"parent","engine":"InnoDB","charset":"utf8mb3","collation":"utf8mb3_general_ci","columns":[{"ordinal_position":1,"name":"id","column_type":"int","nullable":false,"default":"absent","charset":null,"collation":null,"generated_expression":null,"generated_stored":null}],"indexes":[{"name":"PRIMARY","unique":true,"index_type":"BTREE","visible":true,"columns":[{"ordinal_position":1,"column_name":"id","expression":null,"prefix_length":null}]}]}],"foreign_keys":[{"constraint_schema":"app","constraint_name":"fk_child_parent","table_schema":"app","table_name":"child","referenced_table_schema":"app","referenced_table_name":"parent","match_option":"NONE","update_rule":"RESTRICT","delete_rule":"CASCADE","columns":[{"ordinal_position":1,"column_name":"parent_id","referenced_column_name":"id"}]}]}
sha256:ec651d11903da08bbc0092ef468d38d886254e3edb0625cb3105994d91873e20
```
`oneclick.plan` transmits the full typed public `snapshot` and every action's `expected_pre_facts={facts,facts_hash}` / `expected_post_facts={facts,facts_hash}` for safe UI comparison. Snapshot hash covers canonical snapshot bytes; each facts hash covers its document; plan hash covers `{plan_version,target_identity,remediation_profile,snapshot_hash,actions}` including facts. Exact domains are `tunnelforge.oneclick.snapshot.v1\0`, `tunnelforge.oneclick.action-facts.v1\0`, and `tunnelforge.oneclick.plan.v1\0`. Reject credential/connection keys, arbitrary messages, hash/document mismatch, facts outside action tables, or noncanonical order. Approval copies only versions, identity, profile, snapshot hash, and plan hash—never snapshot/actions/facts.

Each `OneClickApplyAction` contains exactly one Rust-generated SQL statement. Charset conversion actions are generated only for isolated tables that do not participate in any foreign key; FK-connected tables remain visible inspection/manual findings because a table-level conversion can reject or widen FK columns. Expand only eligible isolated conversions and every other multi-SQL recommendation into separately ordered actions with scoped expected pre/post facts and optional one-statement rollback. Plan hash commits expanded order; reject empty SQL, SQL arrays, or a nonterminal statement separator.

- [x] **Step 1: Write RED canonical/profile/schema/payload tests**
Test golden vectors/default wire forms, sorting, stable hashes, changed column/generated/index/FK/action/profile/order hashes, one-SQL/action expansion, contiguous ordinals, identity, unsupported profile, and credential absence. Define `normalize_oneclick_schema`: nonempty string, no NUL, no leading/trailing whitespace, no case folding. Facade copies `endpoint.to_payload()`, overwrites nested `database` and `schema` with normalized root schema, then sends exactly root `connection`,`schema`. Rust treats root schema as authoritative, fills absent nested values, and returns `oneclick_schema_mismatch` when either nested value is present but not exact. Test overwrite, equal/absent acceptance, both mismatches, and invalid root.

Rust `parse_oneclick_plan_request` permits only root `connection`,`schema` and endpoint keys `engine`,`host`,`port`,`user`,`password`,`database`,`schema`. Parameterize root/nested rejection of `issues`, `charset_contracts`, target overrides, `actions`, `steps`, `profile`, `remediation_profile`, `approval`, `dry_run`, `backup_confirmed`, and unknown keys with `oneclick_plan_payload_prohibited` and zero SQL.

- [x] **Step 2: Run RED**
Run: `cargo test --manifest-path migration_core\Cargo.toml oneclick_plan --lib`
Expected: FAIL because typed facts/profile/hash and strict plan parser do not exist.

- [x] **Step 3: Implement deterministic generic one-session planning**
Refactor `MysqlInspectAdapter` for borrowed `mysql::PooledConn`. Implement `LiveOneClickSession` and `build_oneclick_plan<S: OneClickPlanningSession>` from the shared trait below; initial `oneclick.plan` and apply replan call this same function. Query identity, profile support, inspect, normalized table definitions, and FKs on one session; build canonical snapshot/action hashes. Never hash client JSON or execute SQL while planning.

- [x] **Step 4: Add plan command/capabilities and commit**
Route `oneclick.plan`; advertise its versions plus exact boolean capabilities `oneclick_exact_plan_enabled` and `oneclick_strong_fence_proven`. Mark `oneclick.run`, `oneclick.recommend`, apply preview, and charset-contract derive deprecated; each returns `oneclick_legacy_preview_disabled` without DB/session/client remediation processing and is absent from preview capabilities. Add a protocol matrix test for every legacy command. Exact-plan is an apply-release predicate, not plan endpoint availability; this plan leaves both predicates false.
Run: `cargo test --manifest-path migration_core\Cargo.toml oneclick_plan --lib`
Run: `cargo test --manifest-path migration_core\Cargo.toml service_hello_advertises_core_protocol --lib`
```powershell
git add migration_core/src/oneclick.rs migration_core/src/schema.rs migration_core/src/protocol.rs
git commit -m "Add canonical One-Click planning"
```

### Task 4: Raw Apply Boundary and Generic Candidate Executor
**Files:**
- Modify: `migration_core/Cargo.toml`, `migration_core/Cargo.lock` (add `base64` for URL-safe no-pad encoding)
- Modify: `migration_core/src/oneclick.rs`, `migration_core/src/protocol.rs`, `migration_core/tests/live_roundtrip.rs`

**Separated interfaces:**
```rust
struct ValidatedOneClickApplyRequest { endpoint: Endpoint, schema: String,
    approval: OneClickApprovalArtifact }
struct OneClickContractError { code: &'static str, applied_ordinals: Vec<u32> }
fn parse_oneclick_apply_request(request: &Request) -> Result<ValidatedOneClickApplyRequest, OneClickContractError>;
trait OneClickPlanningSession {
    fn profile_supported(&mut self, profile: &OneClickRemediationProfile) -> Result<bool, String>;
    fn read_target_identity(&mut self, endpoint: &Endpoint) -> Result<OneClickTargetIdentity, String>;
    fn inspect(&mut self, endpoint: &Endpoint) -> Result<InspectionResult, String>;
    fn read_table_definitions(&mut self, schema: &str) -> Result<Vec<ActionTableDefinitionFact>, String>;
    fn read_fk_facts(&mut self, schema: &str) -> Result<Vec<ActionForeignKeyFact>, String>;
}
trait OneClickApplySession: OneClickPlanningSession {
    fn acquire_advisory_lock(&mut self, key: &str, seconds: u32) -> Result<bool, String>;
    fn release_advisory_lock(&mut self, key: &str) -> Result<(), String>;
    fn read_action_facts(&mut self, action: &OneClickApplyAction) -> Result<ActionFactsDocument, String>;
    fn execute_sql(&mut self, sql: &str) -> Result<(), String>;
}
fn build_oneclick_plan<S: OneClickPlanningSession>(session: &mut S, endpoint: &Endpoint,
    schema: &str) -> Result<OneClickPlanEnvelope, OneClickContractError>;
fn execute_approved_oneclick<S: OneClickApplySession>(session: &mut S,
    validated: &ValidatedOneClickApplyRequest) -> Result<OneClickApplyOutcome, OneClickContractError>;
```
Protocol rejects apply `dry_run=true` as legacy preview before session creation. For false, `parse_oneclick_apply_request` permits only `connection`,`schema`,`dry_run`,`backup_confirmed`,`approval`; validates backup/approval/profile and rejects all client `issues`, `charset_contracts`, target overrides, `actions`, `steps`, `profile`, or `remediation_profile`. `LiveOneClickSession` implements both traits; recording sessions own tests.

DB lock scope is separate from approval identity. Normalize the DB-read UUID to lowercase hyphenated form, then key exactly `"tf1:" + URL_SAFE_NO_PAD(SHA256(b"tunnelforge.oneclick.lock.v1\0" || server_uuid ASCII))`: 47 bytes, under MySQL's 64-byte limit. Route, authenticated user, engine, and schema never enter the key, while approval still compares the complete identity exactly. Test length/alphabet/no padding/determinism, equal key across aliases/users/schemas, and different key for different server UUID.

**Stable Rust codes:** `oneclick_apply_disabled`, `oneclick_legacy_preview_disabled`, `oneclick_apply_payload_prohibited`, `oneclick_schema_mismatch`, `oneclick_backup_required`, `oneclick_approval_required`, `oneclick_approval_version_unsupported`, `oneclick_profile_required`, `oneclick_profile_unsupported`, `oneclick_profile_substitution`, `oneclick_target_changed`, `oneclick_snapshot_changed`, `oneclick_plan_changed`, `oneclick_replan_failed`, `oneclick_nothing_to_apply`, `oneclick_lock_unavailable`, `oneclick_precondition_changed`, `oneclick_postcondition_changed`, `oneclick_outcome_indeterminate`.

- [x] **Step 1: RED raw-boundary protocol tests**
In `protocol.rs`, test missing/malformed backup/approval/version, prohibited fields, schema mismatches, and all four gate combinations. Only true/true may reach a session factory; all others return disabled with count zero. Update the Phase A direct-apply fixture to a valid approval.

- [x] **Step 2: RED generic executor tests**
In `oneclick.rs`, use `RecordingPlanningSession` for initial plan/profile support and `RecordingOneClickSession` for replan, lock, mismatch, zero-action, and exact SQL tests. Assert every action executes one statement, checks its pre-state, then checks expected post-state before the next action. Column/generated/index/FK DDL stale fixtures cover plan-to-replan and replan-to-SQL; post-state drift returns `oneclick_postcondition_changed`. Retain before-first-SQL and between-actions hooks; the latter permits only action 1 SQL.

- [x] **Step 3: Implement candidate and honest hard gate**
Executor reads current DB UUID, acquires the server-global lock, then rereads full identity and replans through the shared planner. Compare identity/profile/snapshot/plan and reject zero actions. For each ordered action: read exact pre-facts, execute its single SQL, read exact post-facts, then continue. Stop on any mismatch and release on every definite exit; never execute approval/client actions.

MySQL `ALTER TABLE` implicitly commits; named locks coordinate only cooperating sessions and an external race remains. Production calls `oneclick_apply_enabled(ONECLICK_EXACT_PLAN_ENABLED, ONECLICK_STRONG_FENCE_PROVEN)` before session creation. This plan leaves both false, so no production execution or partial rollback claim is allowed.

- [x] **Step 4: Live tests, GREEN, and commit**
Configured MySQL tests plan/approve then prove public apply is disabled and unchanged. A test-only generic adapter exercises stale replan and both concurrency windows; between-actions may retain action 1 but no later SQL.
Run: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`
Run: `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip oneclick -- --nocapture`
```powershell
git add migration_core/Cargo.toml migration_core/Cargo.lock migration_core/src/oneclick.rs migration_core/src/protocol.rs migration_core/tests/live_roundtrip.rs
git commit -m "Add gated One-Click approval executor"
```

### Task 5: Python Result API, Plan Model, and Evidence v2
**Files:**
- Create: `src/core/oneclick_approval.py`; modify: `src/core/db_core_facade.py`
- Test: `tests/test_oneclick_approval.py`; modify: `tests/test_db_core_service.py`
- Modify: `scripts/capture-oneclick-dry-run-evidence.py`, `scripts/capture-oneclick-real-execution-evidence.py`, `scripts/capture-oneclick-charset-evidence.py`, `scripts/capture-oneclick-charset-derivation-evidence.py`
- Modify: `scripts/validate-oneclick-dry-run-evidence.py`, `scripts/validate-oneclick-real-execution-evidence.py`, `scripts/validate-oneclick-charset-evidence.py`, `scripts/validate-oneclick-charset-derivation-evidence.py`
- Modify: `tests/test_oneclick_real_execution_capture.py`, `tests/test_oneclick_charset_capture.py`, `tests/test_oneclick_charset_derivation_capture.py`
- Modify: `tests/test_oneclick_dry_run_evidence.py`, `tests/test_oneclick_real_execution_evidence.py`, `tests/test_oneclick_charset_evidence.py`, `tests/test_oneclick_charset_derivation_evidence.py`
- Verify optional gate: `scripts/rust-core-regression-gate.ps1` (`RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE=1`)

`OneClickPlan` strictly parses public snapshot and action pre/post fact documents, validates their hashes/order/secret-free keys, and keeps immutable one-SQL actions. Approval copies only identity/profile/two hashes. Facade methods normalize schema without mutating endpoints. Plan sends only connection/schema; apply sends false dry-run, backup, approval, and no remediation fields once. No UI/capture path sends legacy issues/contracts/steps.

Mutation evidence `report_version:2` contains exact public plan, hash-only approval, and apply metadata. Current captures record definite `oneclick_apply_disabled` and re-raise every other/indeterminate error. Validators may accept apply success only when `service_hello.oneclick_exact_plan_enabled is true` **and** `service_hello.oneclick_strong_fence_proven is true`; if either is false, success is invalid and disabled evidence must show unchanged before/after state, one request, and no client steps.

Dry-run evidence v2 is `{mode:"plan_preview", plan:<full public plan>, approval:null, apply:{attempted:false,request_count:0}}`. Migrate capture from `oneclick.run` to one `plan_oneclick` call; validator rejects legacy commands, approval/apply attempts, client issues/contracts/steps, secret keys, or fact/hash mismatch. Retain and test the optional regression-gate environment switch against the v2 validator.

- [ ] **Step 1: RED parser/facade/compatibility tests**
Test typed snapshot/pre/post docs, hash/order/secret validation, approval omission of docs/actions, schema overwrite, `.payload` only on `request_result`, legacy dict compatibility, normalized capability fields, protocol failures, indeterminate outcomes, and zero retries.

- [ ] **Step 2: RED capture/validator tests**
The three mutation capture/validator families retain definite-disabled filtering and add the two-capability success truth table. The dry-run family emits/validates plan-preview v2 with no approval/apply. All four reject old shapes, stale/public-fact hash mismatch, secrets, legacy commands, client remediation, and retries.

- [ ] **Step 3: Implement, verify, and commit**
Run: `.venv\Scripts\python.exe -m pytest tests\test_oneclick_approval.py tests\test_db_core_service.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_real_execution_capture.py tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_derivation_capture.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_charset_evidence.py tests\test_oneclick_charset_derivation_evidence.py -q`
Optional generated-report gate: `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; .\scripts\rust-core-regression-gate.ps1`
```powershell
git add src/core/oneclick_approval.py src/core/db_core_facade.py tests/test_oneclick_approval.py tests/test_db_core_service.py scripts/capture-oneclick-dry-run-evidence.py scripts/capture-oneclick-real-execution-evidence.py scripts/capture-oneclick-charset-evidence.py scripts/capture-oneclick-charset-derivation-evidence.py scripts/validate-oneclick-dry-run-evidence.py scripts/validate-oneclick-real-execution-evidence.py scripts/validate-oneclick-charset-evidence.py scripts/validate-oneclick-charset-derivation-evidence.py tests/test_oneclick_dry_run_evidence.py tests/test_oneclick_real_execution_capture.py tests/test_oneclick_charset_capture.py tests/test_oneclick_charset_derivation_capture.py tests/test_oneclick_real_execution_evidence.py tests/test_oneclick_charset_evidence.py tests/test_oneclick_charset_derivation_evidence.py
git commit -m "Add One-Click plan and disabled-apply evidence"
```

### Task 6: PyQt Plan Display with Apply Disabled
**Files:**
- Modify: `src/ui/dialogs/oneclick_migration_dialog.py`, `src/ui/dialogs/migration_dialogs.py`
- Modify: `src/core/i18n/legacy_translate.py`, `tests/test_i18n.py`, `tests/test_oneclick_rust_core_gate.py`

- [ ] **Step 1: RED UI sequencing tests**
Render identity/profile/hashes/one-SQL actions/`expected_pre_facts`/`expected_post_facts` without secrets. Test all four combinations of `oneclick_exact_plan_enabled` and `oneclick_strong_fence_proven`: Apply/backup enable only for true/true. Zero-action plans show no-work/no retry. Under true/true, confirmation is Yes/No with No default/Escape; No makes zero calls, Yes starts one immutable-artifact worker. Errors never retry; preserve detach/nonblocking close tests.

- [ ] **Step 2: Implement plan-only UI**
Make worker operations `plan|apply`, each one facade call/no loop. Dry-run/preview and Generate Plan both invoke `plan_oneclick`; no worker invokes legacy commands. Add `ExecutionPlanWidget`. `_sync_execution_controls()` enables backup/Apply only when `oneclick_exact_plan_enabled && oneclick_strong_fence_proven`; otherwise Apply is disconnected/disabled. Tooltips state plan preview works but external DDL is not fenced.

- [ ] **Step 3: Verify and commit**
Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_oneclick_rust_core_gate.py tests\test_oneclick_approval.py tests\test_db_core_service.py tests\test_i18n.py -q`
```powershell
git add src/ui/dialogs/oneclick_migration_dialog.py src/ui/dialogs/migration_dialogs.py src/core/i18n/legacy_translate.py tests/test_oneclick_rust_core_gate.py tests/test_i18n.py
git commit -m "Show exact One-Click plans with apply disabled"
```

### Task 7: Final Review, Evidence, Compatibility, and Status
- [ ] **Step 1: Run full gate**
```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest -q
cargo test --manifest-path migration_core\Cargo.toml
cargo build --manifest-path migration_core\Cargo.toml --release
rg -n "oneclick\.plan|oneclick_legacy|ONECLICK_(REAL_EXECUTION_ENABLED|EXACT_PLAN_ENABLED|STRONG_FENCE_PROVEN)|OneClickPlanningSession|ActionFactsDocument|request_result|outcome_indeterminate|retry" src migration_core tests scripts
```
Expected: full suites/build pass; plan works; both public mutations and PyQt Apply remain disabled; no production path reaches candidate SQL.

- [ ] **Step 2: Review failure modes**
Invoke `superpowers:requesting-code-review`. Audit TF-098 landing/wire errors, legacy preview removal, default serializer vectors, one-SQL pre/post actions, planning/apply traits, schema authority, expanded definitions, server-global key, DDL races, conjunction gates, capture filtering, credentials, process loss, UI lifecycle, and retries. Resolve High/Critical findings and rerun Step 1.

- [ ] **Step 3: Record evidence/status and commit**
Update `docs/oneclick_readiness.md` with plan/profile/facts/approval v1, report v2, TF-098 compatibility, and the unclosed DDL race. Align all `docs/current_status.md` sections but keep TF-097 non-closed until a later strong-fence review flips both gates. Archived evidence remains historical.
```powershell
git add docs/oneclick_readiness.md docs/current_status.md
git commit -m "docs: record disabled One-Click approval evidence"
```

## Self-Review
- [ ] Phase A temporarily preserves dry-run while blocking mutation; Phase B preview uses only `oneclick.plan` and disables every legacy client-derived path.
- [ ] Phase B code/tasks stay blocked until TF-098 implementation, structured error wire, negotiation, compatibility consumers, and status closure all pass.
- [ ] TF-098 retains structural `request_result` plus dict-compatible `request_payload`/`request`, typed indeterminate outcomes, and zero retry.
- [ ] `ColumnDefaultFact` custom serde emits exactly the documented string/singleton-object forms used by golden vectors.
- [ ] Root schema is authoritative; facade overwrites nested database/schema without mutating endpoints and Rust rejects exact mismatches.
- [ ] Plan requests contain only connection/schema; Rust rejects every named client-derived remediation field at root/nested boundaries.
- [ ] Typed definitions cover generated columns, index type/visibility/expression/prefix, ordered FK match/update/delete facts, hashes, vectors, and DDL-stale tests.
- [ ] Every ordered action contains one SQL and independent expected pre/post facts; postcondition failure stops later actions.
- [ ] Charset actions exclude every FK-connected table; those charset findings remain manual until a separately reviewed FK-safe contract exists.
- [ ] Generic initial/replan planning shares `OneClickPlanningSession`; apply extends it, while raw validation retains separate fakes/test ownership.
- [ ] Advisory keys are deterministic server-UUID-only 47-byte base64url across aliases/users; lock/precondition races are tested honestly.
- [ ] Zero-action replan is `oneclick_nothing_to_apply` with zero SQL and visible/no-retry UI behavior.
- [ ] Capture tools record only definite `oneclick_apply_disabled` and re-raise every other or indeterminate failure.
- [ ] Capability naming is uniformly `oneclick_exact_plan_enabled` plus `oneclick_strong_fence_proven`; evidence accepts apply success only when both are true.
- [ ] Rust remains DB owner; apply requires both predicates true, while this plan leaves both false and makes no atomicity claim.
