# One-Click Migration Readiness

This document records the current One-Click release boundary. The Phase A
decision below supersedes the older #138-#141 statements that described a
limited non-dry-run path as currently available.

## Current Decision: Phase A

One-Click is currently a dry-run preview only. Database mutation is disabled at
the Rust protocol, Python worker, PyQt controls, and real-execution evidence
capture boundaries.

- `src\ui\dialogs\migration_dialogs.py` keeps
  `ONE_CLICK_MIGRATION_FEATURE_ENABLED = True` so the preview remains visible.
- `src\ui\dialogs\oneclick_migration_dialog.py` keeps
  `ONECLICK_REAL_EXECUTION_ENABLED = False`.
- Rust `oneclick.run` and `oneclick.apply_fixes` reject every
  `dry_run=false` request before endpoint parsing, adapter creation, or SQL.
  The structured error code is `oneclick_apply_disabled`.
- The Python worker rejects non-dry-run before connector or facade access,
  regardless of backup confirmation.
- The dialog keeps Dry-run checked and disabled. Backup confirmation remains
  disabled, including after a dry-run completes.

Backup confirmation alone is not approval for mutation. Re-enabling apply
requires the Phase B prerequisites below, including exact-plan approval and
completion of TF-STATUS-098.

## Supported Scope Today

The supported scope is intentionally limited to inspection and preview:

- Rust Core remains the sole DB operation owner. Python owns UI, sequencing,
  and rendering only.
- `oneclick.run` with explicit or default dry-run remains available for the
  current preflight, analysis, recommendation, execution-preview, validation,
  and report flow.
- `oneclick.apply_fixes dry_run=true` remains available temporarily for the
  current preview contract.
- `oneclick.derive_charset_contracts` may derive local-safe recommendation
  facts, but those facts cannot authorize apply in Phase A.
- The real-execution capture command does not support non-dry-run execution;
  other mutation evidence must not be refreshed or treated as current support.

## Evidence Classification

The current readiness artifact is:

- `reports\oneclick_readiness\oneclick-dry-run-evidence.json`, which proves the
  dry-run event and report contract without database mutation.

The real-execution, charset execution, and PyQt-triggered derivation mutation
artifacts in `reports\oneclick_readiness` are archived historical evidence.
They preserve controlled local results from the retired open apply path, but
they are not current apply-readiness proof and must not be described as current
live success.

Their validators may still check archive integrity. They do not override the
Phase A protocol gate, satisfy exact-plan approval, close TF-STATUS-098, or
authorize refreshing mutation evidence.

## Automatic Fix Coverage

Rust may still classify and preview remediation candidates. Classification is
not execution approval.

| Issue type | Phase A status | Strategy | Notes |
| --- | --- | --- | --- |
| `deprecated_engine` | recommendation/preview only | `engine_innodb` | Rust may generate a candidate `ALTER TABLE ... ENGINE=InnoDB` action. Both non-dry-run entries reject before SQL. |
| `charset_issue` | recommendation/preview only | `charset_collation_fk_safe` for a complete local-safe contract; otherwise `manual` | Derived target/FK/rollback facts may be displayed, but Phase A does not execute the contract. |
| `invalid_date` | manual | `manual` | Requires value policy and data-loss review. |
| `zerofill_usage` | manual | `manual` | Usually requires application display formatting changes. |
| `float_precision` | manual | `manual` | Requires precision/scale policy review. |
| `int_display_width` | display-only skip | `skip` | Rust Core live One-Click does not synthesize `int_display_width` issues. Externally supplied display-only skip data does not execute SQL. |
| `enum_empty_value` | manual | `manual` | Requires data cleanup policy. |

## Real-Execution Capture Gate

`scripts\capture-oneclick-real-execution-evidence.py` is deliberately disabled
during Phase A. Its independent capture gate fails before local-container
seeding, facade construction, endpoint access, query execution, apply, or
evidence output.

The stable failure contract is:

- code: `oneclick_apply_disabled`
- message: `Phase A disables One-Click real-execution evidence capture; exact-plan approval and TF-STATUS-098 are required before DB mutation.`

Changing the UI feature flag does not bypass the capture gate. Mutation capture
must be explicitly redesigned and reviewed with Phase B rather than silently
resuming the historical request shape.

## Phase B Prerequisites

Non-dry-run must remain disabled until all of the following are true:

1. TF-STATUS-098 is complete with bounded requests, strict request IDs,
   unusable-process reaping, typed indeterminate mutation outcomes, and no
   mutation retry.
2. A canonical, secret-free plan binds current target identity, remediation
   profile, snapshot, ordered actions/preconditions, and plan hash.
3. The user approves that exact plan, and Rust rechecks identity, plan, and
   immediate action preconditions before mutation.
4. The MySQL fencing decision is explicit and the product apply gate remains
   false unless the required strong fence is proven.
5. Capture tooling and evidence semantics are replanned so tests cannot turn a
   synthetic success dictionary into a current live-success claim.

Until then, use dry-run evidence and treat mutation artifacts as archive-only.
