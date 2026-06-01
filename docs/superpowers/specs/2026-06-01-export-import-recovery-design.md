# Export/Import Recovery Design

Date: 2026-06-01

## Objective

Recover TunnelForge Export/Import without temporary patches by making the Rust
core the single owner of DB dump semantics, import execution, validation, and
success/failure classification. Python and PyQt remain responsible for UI,
orchestration, payload forwarding, progress display, and user confirmation.

This design covers the complete recovery path: root-cause fixes, safer import
mode behavior, manifest fidelity, export consistency, verification gates, UI
wording, tests, and final reporting.

## Current Evidence

The review report at `reports/export_import_flow_review_20260601.html`
identified several flow-level risks:

- Safe recreate import can create/load a shadow schema on the main connection
  while parallel TSV workers reconnect through the original endpoint database.
- Export currently lacks a clearly enforced consistent snapshot boundary across
  schema, counts, and chunk data.
- Merge/retry paths can load data and then apply post-load DDL as if the target
  was freshly recreated.
- The UI computes `timezone_sql`, but the wrapper does not forward it to the
  Rust import command payload.
- Import success is not gated by complete schema/data verification.
- UI wording promises object restoration that the Rust dump path does not
  implement for views, procedures, triggers, and events.
- Some existing dumps lack charset/collation or checksum metadata and therefore
  cannot prove exact restoration.

The reported MySQL error `ERROR 3780` must be treated as a schema fidelity and
import plan validation failure, not as an isolated DDL string problem.

## Architecture

Export/Import is a Rust-core-owned dump pipeline.

- `dump.run` creates a restorable artifact. It owns schema inspection, table
  metadata, snapshot policy, row counting, chunk writing, checksums, and
  manifest completeness.
- `dump.import` validates a dump artifact, builds an import plan, executes the
  selected mode, verifies the result, and emits success only after verification.
- `src/exporters/rust_dump_exporter.py` is a JSONL wrapper. It forwards UI
  intent and does not reinterpret database semantics.
- `src/ui/dialogs/db_dialogs.py` collects user intent, presents risks, and
  displays Rust-classified outcomes. It must not promise behavior the Rust core
  cannot perform.
- Import verification writes a machine-readable report beside the dump or import
  log with the final verdict, checked invariants, affected tables, and warnings.

The central invariant is:

> Import success means schema, data, post-load DDL, and verification gates passed.
> A completed data load alone is not success.

## Export Data Flow

1. Preflight determines source engine, version, schema/database name, table set,
   supported objects, unsupported objects, and selected export mode.
2. Snapshot policy is selected before reading schema or data.
   - MySQL strict export should use a consistent snapshot when the engine and
     connection model can support it.
   - If parallel export cannot share a consistent snapshot, strict export must
     fall back to a safe single-snapshot path or classify the artifact as
     non-consistent.
3. Schema capture records enough metadata to recreate compatible tables:
   column type, charset, collation, nullability, defaults, generated columns,
   auto increment, indexes, foreign keys, table collation, and table engine when
   applicable.
4. Data chunks record chunk path, row count, byte size, and SHA-256 checksum.
   New strict dumps require checksums.
5. Export validation checks manifest completeness before reporting an artifact
   as import-ready.

Unsupported objects must be explicit in the manifest and UI. They cannot be
silently dropped while the UI says every object will be recreated.

## Import Data Flow

1. Manifest validation checks version, source/target engine compatibility,
   required schema metadata, required checksums, selected tables, and legacy
   status before any target mutation.
2. Plan generation resolves the effective target context for every operation.
   Shadow import, direct replace, merge, and retry must be distinct plans.
3. Full replacement uses a shadow schema/database when available. Every loader
   worker receives the resolved shadow target context, not the original endpoint
   database.
4. Table creation and data loading are separated from post-load DDL. Indexes are
   applied before foreign keys.
5. Merge and retry modes do not blindly reapply post-load DDL. Existing objects
   are accepted only when their definitions match the manifest.
6. Verification checks row counts, chunk checksums, schema compatibility, and
   post-load constraints before emitting success.
7. Full replacement switches to the restored schema only after verification.
   Cleanup failures are reported separately from import success.

## Failure Policy

Failures are classified before they reach the UI.

- `export_invalid`: The dump artifact cannot prove it is importable. Examples:
  missing required checksum, incomplete schema metadata, unsupported objects
  hidden by UI promises, or failed snapshot requirements.
- `import_plan_invalid`: The requested mode, target, selected tables, and
  manifest cannot produce a safe plan. No target mutation should occur.
- `load_failed`: Chunk load, row count, checksum, or data conversion failed.
  Full replacement must not switch to the restored schema.
- `post_load_validation_failed`: Index, FK, schema, or constraint validation
  failed after data load. This remains an import failure.
- `cleanup_failed`: Primary import succeeded, but cleanup failed. This is a
  warning with explicit cleanup instructions, not a data success failure.

Retry is stateful. It reads an import report or state file and resumes only from
states that are provably safe. Merge retry is blocked unless the existing target
state can be matched to the manifest.

Legacy dumps without strict metadata are not upgraded by guesswork. They may be
offered as limited restoration only, with reduced guarantees clearly displayed.

## UI Semantics

UI labels must match Rust behavior:

- "Full replacement" is recommended only when the strict safe plan can run.
- "All objects recreated" is forbidden unless views, procedures, triggers, and
  events are actually restored by the dump pipeline.
- Legacy/incomplete dumps are shown as limited restoration.
- Rust error classification is preserved in user messages with cause, affected
  scope, and next action.
- Python-computed intent such as timezone setup is forwarded to Rust and applied
  or rejected there.

## Verification Strategy

Rust unit tests must cover:

- MySQL charset/collation and table option preservation.
- Manifest validation for required checksums and schema metadata.
- Legacy dump classification.
- Full replacement worker target resolution to the shadow schema.
- Merge/retry post-load DDL policy.
- Index-before-FK ordering and error classification.

Rust integration-style tests using fake adapters must cover:

- `plan -> create shadow -> load -> apply DDL -> verify -> switch -> cleanup`.
- Load failure prevents switching.
- Validation failure prevents success events.
- Cleanup failure is reported without changing the primary verdict.

Python tests must cover:

- `timezone_sql`, import mode, selected tables, and strict/legacy policy are
  forwarded to Rust.
- UI wording does not overpromise unsupported object restoration.
- Rust classified errors are shown without collapsing them into generic import
  failure text.

Manual verification must run:

- `cargo test --manifest-path migration_core\Cargo.toml`
- `cargo build --manifest-path migration_core\Cargo.toml --release`
- `pytest`
- A legacy/incomplete manifest check against the provided PROD dump when
  available.
- Final HTML report update with findings, fixes, commands, and residual limits.

## Rollout Plan

1. Fix import safety first:
   - Make import target context explicit.
   - Ensure shadow full replacement workers connect to the shadow database.
   - Split merge/retry post-load DDL behavior from recreate behavior.
2. Add manifest and schema fidelity gates:
   - Require checksums for strict imports.
   - Preserve table/column charset and collation metadata.
   - Classify legacy dumps before mutation.
3. Add export consistency policy:
   - Enforce or record snapshot semantics.
   - Prevent strict export from claiming consistency it cannot prove.
4. Add verification gates:
   - Row counts, checksums, schema compatibility, and post-load validation must
     pass before success.
   - Write an import report with the final verdict and evidence.
5. Align UI and wrapper behavior:
   - Forward missing payload fields.
   - Replace overpromising text.
   - Show classified failure messages.
6. Run the full test and build matrix.
7. Update the HTML review report into a final remediation report.

## Completion Criteria

The recovery is complete only when current evidence proves all of the following:

- The `ERROR 3780` class of mismatch is prevented by schema fidelity capture or
  caught before unsafe import mutation.
- Shadow full replacement cannot load data into the original target database.
- Import success events occur only after verification.
- Strict import rejects incomplete manifests or explicitly routes them through a
  limited legacy path.
- UI wording matches actual Rust behavior.
- Rust tests, Rust release build, and Python tests pass or any skipped checks are
  explained with concrete reasons.
- The final HTML report is updated and available through a `file:///...html`
  path.

