# One-Click Migration Readiness

This document tracks the release scope for the hidden One-Click migration UI
gate in GitHub #137.

## Current Decision

One-Click migration remains hidden in the migration analyzer UI.

- `src\ui\dialogs\migration_dialogs.py` keeps
  `ONE_CLICK_MIGRATION_FEATURE_ENABLED = False`.
- `src\ui\dialogs\oneclick_migration_dialog.py` keeps
  `ONECLICK_REAL_EXECUTION_ENABLED = False`.
- The hidden dialog locks Dry-run checked/disabled while the production
  readiness gate is incomplete.

## Supported Scope Today

The current supported scope is evidence collection only:

- Rust Core command contract: `oneclick.run`, `oneclick.preflight`,
  `oneclick.analyze`, `oneclick.recommend`, `oneclick.apply_fixes`,
  `oneclick.validate`, and `oneclick.report`.
- Backend: Rust Core only. Legacy Python-owned One-Click phase orchestration is
  not supported.
- Execution mode: dry-run only.
- Endpoint scope: local MySQL test schema for readiness evidence.
- Evidence artifact:
  `reports\oneclick_readiness\oneclick-dry-run-evidence.json`.

## Not Yet Supported

- User-facing One-Click entry point in the migration analyzer.
- Non-dry-run One-Click execution.
- Production database usage.
- Claiming automatic remediation coverage beyond the Rust Core dry-run event
  contract.

## Gate To Revisit

Before exposing the UI as preview, beta, or fully enabled:

1. Define which issue types can be automatically fixed and which remain manual.
2. Add contract coverage for every additional event payload the UI renders.
3. Capture realistic Rust Core dry-run evidence for the supported scope.
4. Decide and document whether the UI remains hidden, ships as preview/beta, or
   becomes fully enabled.
5. Update the feature flags, user-facing docs, and `docs\current_status.md` in
   the same change if the decision changes.

## 2026-06-26 Analysis

The current evidence supports keeping the workflow hidden or, at most, exposing
a dry-run-only preview after UI copy and tests are tightened. It does not
support full enablement.

Reasons:

- Rust Core `oneclick.run` emits a complete phase/progress/report stream, but
  `oneclick_analysis_summary` reports `auto_fixable = 0` and
  `manual_review = issues.len()`.
- `oneclick_recommendations` currently returns manual review steps only.
- `oneclick_apply_fixes` and the execution phase do not apply SQL fixes; a
  non-dry-run request logs that no automatic Rust Core fixes are currently
  required.
- The migration analyzer button copy and tooltip still say automatic migration
  and automatic fixes, which overstates the current backend behavior.
- The dialog backup checkbox defaults unchecked. If preview is exposed, dry-run
  UX should avoid making a backup warning look like a solved migration issue.

Recommended next repo-side change:

1. Keep `ONECLICK_REAL_EXECUTION_ENABLED = False`.
2. Add a separate preview decision flag or keep
   `ONE_CLICK_MIGRATION_FEATURE_ENABLED = False` until copy is updated.
3. If preview is chosen, expose it as "One-Click dry-run preview" only, update
   tooltip/result wording, and add tests for button visibility plus dry-run-only
   behavior.
