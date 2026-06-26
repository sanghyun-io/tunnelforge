# TunnelForge Current Status

Last reviewed: 2026-06-26

This document is the current repository status index. It separates verified
state from planning documents and lists the next actionable issues.

## Continuity Contract

This file is the canonical handoff document for TunnelForge status work. Any
session that investigates project state, fixes tracked issues, changes recovery
behavior, or changes verification evidence must update this file before ending.

Stable issue IDs use the format `TF-STATUS-###`. Do not renumber IDs. Close an
issue by changing its status and adding evidence; do not delete it unless the
entry was created in error.

Allowed issue statuses:

- `open` - confirmed issue with remaining work.
- `in_progress` - current session is actively changing it.
- `blocked` - cannot continue without external input or environment.
- `fixed_pending_full_verify` - focused fix exists, but broader verification or
  downstream work remains.
- `closed` - verified complete with command evidence.
- `watch` - not actionable now, but should be rechecked when nearby work changes.

## Automatic Update Rules

Update this file automatically when any of these happen:

1. A new issue, risk, doc/code mismatch, disabled feature, or verification gap is
   discovered.
2. A tracked issue is partially fixed, fully fixed, blocked, deprioritized, or
   found invalid.
3. A verification command is run whose result changes or strengthens current
   evidence.
4. A project status document, release/build script, feature flag, or architecture
   boundary changes.
5. A user asks for application status, issue tracking, handoff, roadmap, or next
   work.

Required update fields:

- Update `Last reviewed` if the session materially changes status.
- Add or update an entry in `Issue Tracker`.
- Add command evidence in `Verification Log` when commands are run.
- Add a short entry in `Session Log`.
- Keep `Recommended Execution Order` aligned with open issue priority.

Do not mark an issue `closed` without fresh verification evidence in the same
session. If only focused tests passed, use `fixed_pending_full_verify`.

## Summary

TunnelForge is in a strong build/test state, but not all documented recovery
work is complete. The active architecture baseline is Rust Core ownership of DB
operations through `tunnelforge-core`, with Python/PyQt responsible for UI,
orchestration, signals, and dialogs.

The highest-risk gap is Export/Import Recovery. The design and implementation
plan exist, but the code still lacks several planned guarantees around strict
manifest handling, import verification, and export consistency metadata.

## Verified On 2026-06-26

Commands run locally:

| Check | Result |
| --- | --- |
| `git status --short --branch` | `## main...origin/main`, no local changes before this document |
| `pytest -q` | PASS, 1707 passed, 3 warnings |
| `cargo test --manifest-path migration_core\Cargo.toml` | PASS |
| `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS |
| `python -m compileall -q main.py src tests` | PASS |
| `git diff --check` | PASS |
| `tunnelforge-core service.hello` | PASS, reports `dump.run`, `dump.import`, migration commands |

Version references are aligned at `2.1.6` across:

- `src/version.py`
- `pyproject.toml`
- `installer/TunnelForge.iss`

## Verification Log

| Date | Scope | Command | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-06-26 | Full Python suite | `pytest -q` | PASS | 1707 passed, 3 warnings |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | Unit, CLI, and gated live tests pass or skip according to env |
| 2026-06-26 | Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Produced `migration_core\target\release\tunnelforge-core.exe` |
| 2026-06-26 | Python syntax | `python -m compileall -q main.py src tests` | PASS | No compile errors |
| 2026-06-26 | Diff hygiene | `git diff --check` | PASS | No whitespace errors |
| 2026-06-26 | Core smoke | `tunnelforge-core service.hello` | PASS | Advertises dump/import and migration commands |
| 2026-06-26 | Import wrapper/dialog focused tests | `python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 62 passed after payload/UI wording fixes |
| 2026-06-26 | Rust timezone validation TDD | `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_session_time_zone_only --lib` | FAIL then PASS | Initial RED failed because `validated_timezone_sql` did not exist; GREEN passed after helper implementation |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 139 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 62 passed after Rust timezone change |
| 2026-06-26 | Strict manifest classification TDD | `cargo test --manifest-path migration_core\Cargo.toml strict_manifest_validation_rejects_missing_chunk_checksums --lib` | FAIL then PASS | Initial RED failed because strictness/classification helpers did not exist; GREEN covered strict reject, legacy warning, classified formatting |
| 2026-06-26 | Strict import wiring | `cargo test --manifest-path migration_core\Cargo.toml dump_import_strict_manifest_rejects_missing_checksums_before_connect --lib` | PASS | Confirms strict import fails before dummy DB connection |
| 2026-06-26 | Classified error wrapper | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py::TestRustDumpImporter::test_import_dump_preserves_classified_core_error -q` | PASS | Confirms `export_invalid` and scope survive Python wrapper |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 143 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 63 passed after classified error regression |
| 2026-06-26 | Rust format and diff hygiene | `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` | PASS | No formatting or whitespace issues |

## Existing Status And Planning Documents

Use these documents as inputs, not as proof of completion:

- `AGENTS.md` - repository operating guidelines and active Rust Core baseline.
- `CLAUDE.md` - broader architecture notes and release workflow notes.
- `docs/cross_engine_migration_plan.md` - cross-engine migration plan plus Rust
  Core transition audit notes.
- `docs/macos_support.md` - macOS support scope and final real-Mac validation
  gates.
- `docs/superpowers/specs/2026-05-19-rust-core-export-progress-performance-design.md`
- `docs/superpowers/plans/2026-05-19-rust-core-export-progress-performance.md`
- `docs/superpowers/specs/2026-05-20-db-conversion-wizard-design.md`
- `docs/superpowers/plans/2026-05-20-db-conversion-guided-wizard.md`
- `docs/superpowers/specs/2026-06-01-export-import-recovery-design.md`
- `docs/superpowers/plans/2026-06-01-export-import-recovery.md`

The three `docs/superpowers/plans/*.md` files are implementation plans with
unchecked task lists. They should not be interpreted as completed work.

## Confirmed Strengths

- Python and Rust test suites are large and currently pass.
- Rust Core JSONL service advertises the expected DB capabilities.
- The packaged app path is guarded by tests for Rust Core inclusion.
- Cross-engine migration UI has focused tests around guided wizard state,
  target approval, cleanup planning, and verify-step flow.
- Python DB connector shims route through `DbCoreFacade` and `RustDbConnection`
  rather than importing direct Python MySQL/PostgreSQL drivers.
- `git ls-files` does not show tracked build/cache artifacts such as
  `__pycache__`, `dist`, `build`, `output`, or `migration_core/target`.

## High Priority Issues

### TF-STATUS-001: Initial Import Intent And Strictness Gates

Status: `closed`
Severity: High
Area: Export/Import Recovery

Evidence:

- 2026-06-26 update: Python now forwards `timezone_sql` and
  `strict_manifest=True` to the Rust import payload, with focused pytest
  coverage.
- 2026-06-26 update: Rust now validates `timezone_sql` as a single
  `SET SESSION time_zone` statement with a literal value and applies it on the
  import adapter session immediately after connection.
- 2026-06-26 update: Rust now rejects strict imports with missing
  `chunk_sha256` metadata before DB connection/target mutation; non-strict
  legacy imports emit warning events.
- 2026-06-26 update: classified Rust import errors are preserved through the
  Python import wrapper message.

Impact:

- Initial import intent and strictness gates are now enforced at the Rust Core
  boundary instead of only being represented in Python payloads.
- Remaining recovery plan work is tracked separately by `TF-STATUS-002` and
  `TF-STATUS-004`.

Next action:

1. Continue import success gating in `TF-STATUS-002`.
2. Add export consistency manifest metadata in `TF-STATUS-004`.

### TF-STATUS-002: Import Success Is Not Gated By Complete Verification

Status: `open`
Severity: High
Area: Rust Core dump.import

Evidence:

- `dump_import()` validates existing chunks only when checksums are present.
- `dump_import()` applies post-load DDL and then returns a `success: true`
  result without a row-count verification report.
- `DumpManifest` does not contain `snapshot_policy`, `strict_export`, or
  `manifest_warnings`.

Impact:

- A data-load completion can be reported as import success without proving all
  planned recovery invariants.
- Legacy or incomplete dumps are not clearly classified before target mutation.

Next action:

1. Add strict manifest validation helpers and tests.
2. Track imported rows per table.
3. Gate success on row-count verification.
4. Write `_tunnelforge_import_report.json` beside the dump.

### TF-STATUS-003: Import UI Overpromises Object Restoration

Status: `fixed_pending_full_verify`
Severity: High
Area: Import UI

Evidence:

- 2026-06-26 update: the import dialog no longer says `모든 객체`.
- The dialog now describes table structure/data recreation and states that
  View restoration is best effort while procedures/triggers/events require
  separate confirmation.

Impact:

- This specific overpromise is fixed, but broader unsupported object reporting
  still belongs in the remaining Recovery work.

Next action:

1. Keep the regression test that rejects `모든 객체`.
2. Continue surfacing unsupported object warnings from Rust in the import UI.

### TF-STATUS-004: Export Consistency Is Not Explicit In The Manifest

Status: `open`
Severity: High
Area: Rust Core dump.run manifest

Evidence:

- The recovery design requires strict export metadata such as snapshot policy.
- Current `DumpManifest` lacks fields to distinguish strict consistent exports
  from non-consistent parallel exports.

Impact:

- A dump artifact cannot communicate whether it can prove a shared consistency
  boundary across schema, counts, chunks, and checksums.

Next action:

1. Add manifest fields for `snapshot_policy`, `strict_export`, and
   `manifest_warnings`.
2. Default legacy manifests to `snapshot_policy = "unknown"`.
3. Mark new exports according to the actual export path.

## Medium Priority Issues

### TF-STATUS-005: Public Docs Mention Features Disabled In Main UI

Status: `open`
Severity: Medium
Area: Docs/UI feature flags

Evidence:

- `SCHEDULE.md` documents scheduled backup flows.
- `src/ui/main_window.py` sets `SCHEDULE_FEATURE_ENABLED = False`.
- `src/ui/main_window.py` sets `SQL_FILE_EXECUTION_FEATURE_ENABLED = False`.

Impact:

- User-facing capability expectations can diverge from the app surface.

Next action:

1. Decide whether scheduled backup and SQL file execution are supported,
   hidden, or retired.
2. Update docs or feature flags accordingly.

### TF-STATUS-006: Large Files Increase Change Risk

Status: `watch`
Severity: Medium
Area: Maintainability

Largest implementation files:

- `migration_core/src/lib.rs` - about 11,600 lines.
- `src/ui/dialogs/sql_editor_dialog.py` - about 3,209 lines.
- `src/ui/dialogs/db_dialogs.py` - about 3,078 lines.
- `src/ui/dialogs/cross_engine_migration_dialog.py` - about 1,630 lines.

Impact:

- Focused fixes are possible, but refactors and behavior changes have a broad
  blast radius.

Next action:

1. Keep recovery fixes narrowly scoped and test-first.
2. Defer structural splitting until behavior is stabilized.

## Lower Priority / Tracking

### TF-STATUS-007: Referenced Export/Import HTML Report Is Missing

Status: `open`
Severity: Low
Area: Documentation/reporting

Evidence:

- `reports/export_import_flow_review_20260601.html` is referenced by the
  recovery design and plan.
- The repository currently has no `reports/` directory.

Next action:

1. Create the final remediation report after recovery work is implemented and
   verified, or update the design/plan to point to the correct report location.

### TF-STATUS-008: macOS Support Still Requires Real-Mac Final Validation

Status: `watch`
Severity: Low
Area: macOS release readiness

Evidence:

- `docs/macos_support.md` explicitly states final real-Mac validation is
  separate from repository verification.

Next action:

1. Do not call macOS support production-ready until the final manual validation
   evidence bundle exists.

## Issue Tracker

| ID | Severity | Status | Area | Short Title | Next Action |
| --- | --- | --- | --- | --- | --- |
| TF-STATUS-001 | High | closed | Export/Import Recovery | Initial import intent and strictness gates | Continue remaining recovery work in TF-STATUS-002 and TF-STATUS-004 |
| TF-STATUS-002 | High | open | Rust Core import | Import success not fully verified | Add row-count verification and import report artifact |
| TF-STATUS-003 | High | fixed_pending_full_verify | Import UI | Object restoration wording | Keep focused regression; add unsupported-object UI surfacing |
| TF-STATUS-004 | High | open | Rust Core export | Export consistency not explicit | Add snapshot/strictness manifest metadata |
| TF-STATUS-005 | Medium | open | Docs/UI flags | Docs mention disabled features | Decide support status for schedule and SQL file execution |
| TF-STATUS-006 | Medium | watch | Maintainability | Very large files | Keep fixes narrow; split later if behavior stabilizes |
| TF-STATUS-007 | Low | open | Reporting | Referenced HTML report missing | Create/update final remediation report |
| TF-STATUS-008 | Low | watch | macOS | Final real-Mac validation pending | Require evidence bundle before production-ready claim |

## Recommended Execution Order

1. Add Rust import verification:
   - per-table imported row counts
   - success only after verification
   - import report artifact
2. Add export strictness metadata:
   - snapshot policy
   - strict export marker
   - manifest warnings
3. Update or create final remediation report after the recovery work is
   verified.

## Session Log

| Date | Session Summary | Files Touched | Verification |
| --- | --- | --- | --- |
| 2026-06-26 | Created canonical status inventory after full repo survey. | `docs/current_status.md` | `pytest -q`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `compileall`; `git diff --check`; `service.hello` |
| 2026-06-26 | Added Python import payload forwarding for `timezone_sql` and `strict_manifest`; removed import UI `모든 객체` overpromise; added focused regression tests. | `src/exporters/rust_dump_exporter.py`, `src/ui/dialogs/db_dialogs.py`, `tests/test_rust_dump_exporter.py`, `tests/test_db_dialogs.py`, `docs/current_status.md` | `python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `git diff --check` |
| 2026-06-26 | Added Rust validation/application for dump import `timezone_sql`; arbitrary SQL and multi-statement payloads are rejected before DB connection. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_session_time_zone_only --lib`; `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` |
| 2026-06-26 | Added strict manifest classification before dump import target mutation and preserved classified core errors through Python import messages. | `migration_core/src/lib.rs`, `tests/test_rust_dump_exporter.py`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
