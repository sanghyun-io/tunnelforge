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
  `oneclick.analyze`, `oneclick.recommend`, `oneclick.apply_fixes`,
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

## Not Yet Supported

- Production database usage.
- Automatic remediation coverage beyond
  `deprecated_engine -> engine_innodb`.

## Automatic Fix Coverage

Current Rust Core recommendation coverage:

| Issue type | Status | Strategy | Notes |
| --- | --- | --- | --- |
| `deprecated_engine` | automatic candidate | `engine_innodb` | Generates `ALTER TABLE <schema>.<table> ENGINE=InnoDB;` when `schema` and `table_name` are present. `oneclick.apply_fixes` and UI-facing `oneclick.run dry_run=false` execute only this strategy through Rust Core. PyQt requires backup confirmation before sending a non-dry-run payload. |
| `charset_issue` | manual | `manual` | FK-safe ordering, rollback, and collation/charset target selection must be proven before automatic execution. |
| `invalid_date` | manual | `manual` | Requires value policy and data-loss review. |
| `zerofill_usage` | manual | `manual` | Usually requires application display formatting changes. |
| `float_precision` | manual | `manual` | Requires precision/scale policy review. |
| `int_display_width` | manual or skip | `manual` | MySQL 8.4 ignores display width semantics; no automatic DDL is currently applied. |
| `enum_empty_value` | manual | `manual` | Requires data cleanup policy. |

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
