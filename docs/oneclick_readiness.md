# One-Click Migration Readiness

This document tracks the release scope for the One-Click migration UI gate.
GitHub #137 covered dry-run preview exposure; GitHub #138 tracks real execution
and automatic fix coverage.

## Current Decision

One-Click migration is exposed in the migration analyzer UI with dry-run enabled
by default. Limited real execution is available only after backup confirmation
and only for the validated `deprecated_engine -> engine_innodb` path.

- `src\ui\dialogs\migration_dialogs.py` keeps
  `ONE_CLICK_MIGRATION_FEATURE_ENABLED = True`.
- `src\ui\dialogs\oneclick_migration_dialog.py` keeps
  `ONECLICK_REAL_EXECUTION_ENABLED = True`.
- The dialog keeps Dry-run checked by default. If Dry-run is disabled, the
  worker fails closed unless `backup_confirmed=true`.

## Supported Scope Today

The current supported scope is intentionally narrow:

- Rust Core command contract: `oneclick.run`, `oneclick.preflight`,
  `oneclick.analyze`, `oneclick.recommend`,
  `oneclick.derive_charset_contracts`, `oneclick.apply_fixes`,
  `oneclick.validate`, and `oneclick.report`.
- Backend: Rust Core only. Legacy Python-owned One-Click phase orchestration is
  not supported.
- Execution mode: dry-run by default; non-dry-run may apply only the validated
  `deprecated_engine -> engine_innodb` automatic fix.
- Safety gate: non-dry-run requires backup confirmation from the UI payload.
- Endpoint scope: local MySQL test schema for readiness evidence.
- Evidence artifact:
  `reports\oneclick_readiness\oneclick-dry-run-evidence.json`.
- Real-execution evidence contract:
  `scripts\validate-oneclick-real-execution-evidence.py` and
  `reports\oneclick_readiness\oneclick-real-execution-evidence.json`.
  Completed evidence proves Rust Core `oneclick.apply_fixes` can convert the
  controlled local test table
  `tf_oneclick_real_execution.tf_oneclick_legacy_engine_table` from `MyISAM` to
  `InnoDB`. A live Rust Core regression also proves `oneclick.run dry_run=false`
  sequences the same validated apply path.
- Charset/collation evidence contract:
  `scripts\validate-oneclick-charset-evidence.py` and
  `reports\oneclick_readiness\oneclick-charset-evidence.json`.
  Completed evidence proves Rust Core `oneclick.apply_fixes` can convert the
  controlled local FK-connected test tables
  `tf_oneclick_charset.tf_oneclick_parent` and
  `tf_oneclick_charset.tf_oneclick_child` from `utf8mb3` /
  `utf8mb3_general_ci` to `utf8mb4` / `utf8mb4_0900_ai_ci`, while preserving
  FK evidence and rollback metadata. A live Rust Core regression also proves
  UI-facing `oneclick.run dry_run=false` executes the same supplied complete
  contract shape.
- Charset/collation capture helper:
  `scripts\capture-oneclick-charset-evidence.py` seeds and captures only safe
  local `tf_oneclick_` scopes through Rust DB Core APIs.
- Charset/collation derivation command:
  `oneclick.derive_charset_contracts` can derive complete local-safe
  `charset_contracts[]` from supplied Rust-owned table/FK facts or from live
  MySQL `information_schema` facts. PyQt calls this command before
  `oneclick.run` and includes derived issues/contracts only when the derivation
  gate returns both.

## Not Yet Supported

- Production database usage.
- Production-ready automatic PyQt charset contract derivation evidence.
  GitHub #140 tracks the remaining local evidence and closure work.

## Automatic Fix Coverage

Current Rust Core recommendation coverage:

| Issue type | Status | Strategy | Notes |
| --- | --- | --- | --- |
| `deprecated_engine` | automatic candidate | `engine_innodb` | Generates `ALTER TABLE <schema>.<table> ENGINE=InnoDB;` when `schema` and `table_name` are present. `oneclick.apply_fixes` and UI-facing `oneclick.run dry_run=false` execute only this strategy through Rust Core. PyQt requires backup confirmation before sending a non-dry-run payload. |
| `charset_issue` | local contract allowlisted, PyQt derivation pending | `charset_collation_fk_safe` when a complete contract is supplied; otherwise `manual` | Rust Core can classify and execute charset fixes only when the request includes complete safe contract data: safe `tf_oneclick_` identifiers, explicit target charset/collation, FK order covering the conversion set, and rollback SQL. `oneclick.apply_fixes dry_run=false` and UI-facing `oneclick.run dry_run=false` execute that supplied contract through Rust Core; PyQt rendering/count copy is covered, while automatic contract derivation is tracked by #140. |
| `invalid_date` | manual | `manual` | Requires value policy and data-loss review. |
| `zerofill_usage` | manual | `manual` | Usually requires application display formatting changes. |
| `float_precision` | manual | `manual` | Requires precision/scale policy review. |
| `int_display_width` | manual or skip | `manual` | MySQL 8.4 ignores display width semantics; no automatic DDL is currently applied. |
| `enum_empty_value` | manual | `manual` | Requires data cleanup policy. |

## Charset/Collation Automation Policy (#139)

Charset/collation command-level execution is allowlisted only for complete
`charset_collation_fk_safe` contracts. Local MySQL evidence for the command
path is captured and validator-backed. UI-facing `oneclick.run dry_run=false`
can execute the same supplied complete contract shape; automatic PyQt contract
derivation is tracked separately in GitHub #140. The following policy defines
the only eligible scope.

Eligible automatic subset:

- Issue type must be exactly `charset_issue`.
- Strategy must be exactly `charset_collation_fk_safe`.
- Execution must be table-level only:
  `ALTER TABLE <schema>.<table> CONVERT TO CHARACTER SET <target> COLLATE <target>;`.
- The target must be explicit in the request/evidence. The current planned
  target is `utf8mb4` with `utf8mb4_0900_ai_ci`.
- Every table name must use the safe local evidence prefix `tf_oneclick_` while
  the feature is being proven.
- Every table in the FK-connected conversion set must be present in the
  evidence `tables` list and in `fk_order`.
- Before evidence must prove each converted table was not already at the target
  charset/collation.
- After evidence must prove every converted table reached the target
  charset/collation and that FK constraints remain valid.
- Rollback metadata and rollback SQL must be captured before the real
  conversion is considered valid evidence.

Manual or fail-closed subset:

- Column-level charset/collation changes.
- Mixed or ambiguous target charset/collation.
- Missing FK dependency ordering or FK closure information.
- Missing rollback metadata.
- Any table outside the controlled local `tf_oneclick_` evidence namespace.
- Any production database evidence or production execution claim.
- Any charset/collation strategy other than `charset_collation_fk_safe`.
- Any bundled remediation with invalid dates, ZEROFILL, float precision,
  integer display width, enum cleanup, or other issue types.

Implementation gate:

- Rust Core recommendation changes must not mark `charset_issue` as
  `auto_fixable` until `oneclick.apply_fixes` and UI-facing
  `oneclick.run dry_run=false` can execute or fail closed for the exact
  allowlisted strategy.
- The optional regression gate
  `RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE=1` must pass against
  `reports\oneclick_readiness\oneclick-charset-evidence.json` before
  `charset_issue` is added to the real-execution allowlist.
- `scripts\capture-oneclick-charset-evidence.py` captures live evidence only
  from local `tf_oneclick_` evidence scopes.
- Rust Core now uses the `charset_collation_fk_safe` contract helper for
  `oneclick.recommend` when `charset_contracts[]` includes a complete contract
  for the issue index. Missing or incomplete contract data keeps the issue
  manual.
- `oneclick.apply_fixes dry_run=true` can return charset `planned_fixes` for a
  complete contract without executing SQL. `oneclick.apply_fixes dry_run=false`
  can execute the contract SQL through the Rust adapter path and includes
  rollback metadata in the applied-fix payload; validator-backed live MySQL
  evidence for this command path is captured.
- UI-facing `oneclick.run dry_run=false` now merges supplied payload
  `issues[]` and `charset_contracts[]`, shifts contract indexes behind
  inspection-derived issues, and sequences the same allowlisted apply path for
  complete local-safe charset contracts.

## Real-Execution Gate Outcome

The dry-run lock was removed only for the first validated automatic-fix scope.
The following gate criteria are complete for
`deprecated_engine -> engine_innodb`:

1. Define which issue types can be automatically fixed and which remain manual.
2. Add contract coverage for every additional event payload the UI renders.
3. Capture realistic Rust Core non-production real-execution evidence for the
   supported automatic-fix scope. Done for `oneclick.apply_fixes` /
   `engine_innodb` in
   `reports\oneclick_readiness\oneclick-real-execution-evidence.json`.
4. Validate that evidence with
   `scripts\validate-oneclick-real-execution-evidence.py`. Done for the
   archived local evidence.
5. Decide and document how `oneclick.run` should invoke/sequence automatic
   fixes, because the UI currently calls `oneclick.run`, not
   `oneclick.apply_fixes` directly. Done for the validated
   `deprecated_engine -> engine_innodb` path.
6. Decide and document whether the UI remains dry-run preview/beta or becomes
   real-execution capable. Done: real execution is enabled only for the limited
   `engine_innodb` path, while Dry-run remains the default.
7. Update the feature flags, user-facing docs, and `docs\current_status.md` in
   the same change if the decision changes. Done.

The #138 real-execution evidence validator currently requires:

- `issue: 138`, `source_type: local_mysql_container`, and a safe
  `tf_oneclick_` schema.
- the recorded `ONECLICK_REAL_EXECUTION_ENABLED` feature flag as a boolean
  audit field. Archived evidence may show `false` because it was captured
  before the UI gate opened; refreshed evidence may show `true`.
- all `oneclick.*` Rust Core service capabilities.
- `oneclick.apply_fixes` with `dry_run=false`, `backup_confirmed=true`, and no
  disallowed fix attempts.
- attempted and applied scope limited to
  `deprecated_engine -> engine_innodb`.
- before/after table evidence proving a deprecated non-InnoDB engine changed to
  `InnoDB` and unrelated tables were unchanged.
- final validation showing `all_fixed=true`, `remaining_issues=0`, and
  `post_engine=InnoDB`.

## 2026-06-26 Analysis

The current evidence supports exposing One-Click with dry-run default and a
limited real-execution path. It does not support broad automatic remediation.

Reasons:

- Rust Core MySQL inspection emits `deprecated_engine:<table>:<engine>` markers
  for MyISAM base tables, and One-Click converts those markers into typed
  `deprecated_engine` issues that can be recommended as `engine_innodb`.
- `oneclick_recommendations` currently marks only `deprecated_engine` payload
  issues with `table_name` as automatic candidates.
- `oneclick_apply_fixes` now plans and executes only the allowed
  `deprecated_engine -> engine_innodb` action through Rust Core
  `MigrationAdapter::execute_sql`; manual/skip steps remain skipped, disallowed
  strategies are blocked, and missing endpoints fail closed.
- `reports\oneclick_readiness\oneclick-real-execution-evidence.json` now proves
  `oneclick.apply_fixes` changed the controlled local MySQL table from
  `MyISAM` to `InnoDB`.
- The full `oneclick.run` execution phase now sequences the same
  `engine_innodb` apply plan when `dry_run=false`, and a live MySQL regression
  test proves it converts a MyISAM table to InnoDB.
- App-level real execution is now enabled only for this limited path. The PyQt
  worker still fails closed if Dry-run is disabled without backup confirmation.
- `scripts\validate-oneclick-real-execution-evidence.py` defines the
  machine-checkable proof required before `engine_innodb` real execution can be
  considered ready; the current archived evidence passes that validator for
  `oneclick.apply_fixes`.
- `scripts\validate-oneclick-charset-evidence.py` now defines the
  machine-checkable proof required before any
  `charset_issue -> charset_collation_fk_safe` real execution can be enabled.
  It requires a safe `tf_oneclick_` schema, local MySQL source, explicit target
  charset/collation, FK-valid after-state, rollback metadata, no disallowed
  attempts, and before/after table charset/collation proof.
- The migration analyzer button copy and tooltip now label the entry point as
  One-Click Migration, say dry-run is the default, and limit automatic changes
  to verified MyISAM/deprecated engine tables becoming InnoDB after backup
  confirmation.
- The dialog backup checkbox defaults unchecked. If preview is exposed, dry-run
  UX should avoid making a backup warning look like a solved migration issue.

Recommended next repo-side change:

1. Keep the current limited-scope real-execution gate narrow.
2. Create separate issues for each additional automatic fix class before
   enabling it.
3. Refresh evidence if event payloads, Rust Core sequencing, or UI safety gates
   change.

Follow-up tracking:

- Dry-run preview gate: GitHub #137.
- Real execution and automatic fix coverage: GitHub #138.
- Charset/collation automatic fix coverage: GitHub #139.
