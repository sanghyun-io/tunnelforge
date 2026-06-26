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
