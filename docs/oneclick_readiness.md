# One-Click Migration Readiness

This document tracks the release scope for the One-Click migration UI gate.
GitHub #137 covered dry-run preview exposure; GitHub #138 now tracks real
execution and automatic fix coverage.

## Current Decision

One-Click migration is exposed as a dry-run-only preview in the migration
analyzer UI.

- `src\ui\dialogs\migration_dialogs.py` keeps
  `ONE_CLICK_MIGRATION_FEATURE_ENABLED = True`.
- `src\ui\dialogs\oneclick_migration_dialog.py` keeps
  `ONECLICK_REAL_EXECUTION_ENABLED = False`.
- The dialog locks Dry-run checked/disabled while the real-execution readiness
  gate is incomplete.

## Supported Scope Today

The current supported scope is dry-run preview only:

- Rust Core command contract: `oneclick.run`, `oneclick.preflight`,
  `oneclick.analyze`, `oneclick.recommend`, `oneclick.apply_fixes`,
  `oneclick.validate`, and `oneclick.report`.
- Backend: Rust Core only. Legacy Python-owned One-Click phase orchestration is
  not supported.
- Execution mode: dry-run only; no SQL fixes are applied.
- Endpoint scope: local MySQL test schema for readiness evidence.
- Evidence artifact:
  `reports\oneclick_readiness\oneclick-dry-run-evidence.json`.

## Not Yet Supported

- Non-dry-run One-Click execution.
- Production database usage.
- Claiming automatic remediation coverage beyond the Rust Core dry-run event
  contract.

## Automatic Fix Coverage

Current Rust Core recommendation coverage:

| Issue type | Status | Strategy | Notes |
| --- | --- | --- | --- |
| `deprecated_engine` | automatic candidate | `engine_innodb` | Generates `ALTER TABLE <schema>.<table> ENGINE=InnoDB;` when `schema` and `table_name` are present. This is recommendation metadata only while real execution remains disabled. |
| `charset_issue` | manual | `manual` | FK-safe ordering, rollback, and collation/charset target selection must be proven before automatic execution. |
| `invalid_date` | manual | `manual` | Requires value policy and data-loss review. |
| `zerofill_usage` | manual | `manual` | Usually requires application display formatting changes. |
| `float_precision` | manual | `manual` | Requires precision/scale policy review. |
| `int_display_width` | manual or skip | `manual` | MySQL 8.4 ignores display width semantics; no automatic DDL is currently applied. |
| `enum_empty_value` | manual | `manual` | Requires data cleanup policy. |

## Real-Execution Gate

Before removing the dry-run lock or enabling real execution:

1. Define which issue types can be automatically fixed and which remain manual.
2. Add contract coverage for every additional event payload the UI renders.
3. Capture realistic Rust Core non-production real-execution evidence for the
   supported automatic-fix scope.
4. Decide and document whether the UI remains dry-run preview/beta or becomes
   real-execution capable.
5. Update the feature flags, user-facing docs, and `docs\current_status.md` in
   the same change if the decision changes.

## 2026-06-26 Analysis

The current evidence supports exposing a dry-run-only preview. It does not
support full enablement.

Reasons:

- Rust Core `oneclick.run` emits a complete phase/progress/report stream, but
  live-inspection issues still do not carry typed automatic-fix metadata.
- `oneclick_recommendations` currently marks only `deprecated_engine` payload
  issues with `table_name` as automatic candidates.
- `oneclick_apply_fixes` and the execution phase do not apply SQL fixes; a
  non-dry-run request logs that no automatic Rust Core fixes are currently
  required.
- The migration analyzer button copy and tooltip now label the entry point as
  dry-run preview and avoid automatic-fix claims.
- The dialog backup checkbox defaults unchecked. If preview is exposed, dry-run
  UX should avoid making a backup warning look like a solved migration issue.

Recommended next repo-side change:

1. Keep `ONECLICK_REAL_EXECUTION_ENABLED = False`.
2. Keep the migration analyzer entry point labeled as dry-run preview.
3. Do not remove the dry-run lock or enable automatic SQL fixes until Rust Core
   defines, implements, and proves automatic fix coverage.

Follow-up tracking:

- Dry-run preview gate: GitHub #137.
- Real execution and automatic fix coverage: GitHub #138.
