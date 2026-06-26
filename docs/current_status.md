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
| 2026-06-26 | Import row-count verification TDD | `cargo test --manifest-path migration_core\Cargo.toml import_row_count_verification --lib` | FAIL then PASS | Initial RED failed because `verify_imported_row_counts` and report path helper did not exist; GREEN covered matching and mismatched table row counts |
| 2026-06-26 | Import report path | `cargo test --manifest-path migration_core\Cargo.toml import_report_path_lives_inside_dump_directory --lib` | PASS | Confirms report path resolves under dump directory |
| 2026-06-26 | Import report artifact | `cargo test --manifest-path migration_core\Cargo.toml write_dump_import_report_creates_json_file --lib` | PASS | Confirms `_tunnelforge_import_report.json` is written with verification JSON |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 147 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 63 passed after import verification/report change |
| 2026-06-26 | Export manifest metadata TDD | `cargo test --manifest-path migration_core\Cargo.toml dump_manifest_strictness_fields_default_for_legacy_json --lib` | FAIL then PASS | Initial RED failed because `snapshot_policy`, `strict_export`, and `manifest_warnings` did not exist |
| 2026-06-26 | Export consistency policy | `cargo test --manifest-path migration_core\Cargo.toml dump_manifest_consistency_metadata --lib` | PASS | Parallel exports are marked non-strict; single-thread exports are marked connection-consistent |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 150 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Import wrapper/dialog focused tests | `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` | PASS | 63 passed after export metadata change |
| 2026-06-26 | Merge post-load DDL policy TDD | `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_policy --lib` | FAIL then PASS | Initial RED failed because merge/recreate DDL policy helpers did not exist |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 152 lib tests, 1 JSONL CLI test, 6 live-roundtrip tests, doctests |
| 2026-06-26 | Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Release Rust core binary builds |
| 2026-06-26 | Python syntax | `.venv\Scripts\python -m compileall -q main.py src tests` | PASS | No compile errors |
| 2026-06-26 | Full Python suite | `.venv\Scripts\python -m pytest -q` | FAIL then PASS | Initial failure exposed missing English translation for new import UI wording; final run passed 1710 tests with 3 warnings |
| 2026-06-26 | Final remediation report | `Get-Item reports\export_import_flow_review_20260601.html` | PASS | Report exists, length 6104 bytes |

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
- Remaining release-readiness watch items are tracked separately.

Next action:

1. Keep the regression tests and report aligned if import intent handling
   changes.

### TF-STATUS-002: Import Success Is Gated By Row Verification

Status: `closed`
Severity: High
Area: Rust Core dump.import

Evidence:

- 2026-06-26 update: `dump_import()` now tracks imported rows per table and
  calls `verify_imported_row_counts()` before returning success.
- 2026-06-26 update: mismatched imported row counts fail with
  `post_load_validation_failed` and table scope.
- 2026-06-26 update: successful imports write
  `_tunnelforge_import_report.json` beside the dump and include the report path
  plus verification summary in the result payload.

Impact:

- Import success now has an explicit row-count verification gate and persisted
  report artifact.
- Export consistency metadata is tracked separately and is now closed.

Next action:

1. Keep row verification and report artifact coverage when import modes change.

### TF-STATUS-003: Import UI Overpromises Object Restoration

Status: `closed`
Severity: High
Area: Import UI

Evidence:

- 2026-06-26 update: the import dialog no longer says `모든 객체`.
- The dialog now describes table structure/data recreation and states that
  View restoration is best effort while procedures/triggers/events require
  separate confirmation.

Impact:

- The overpromising object restoration wording is fixed and verified by the
  focused UI regression plus the full Python suite.
- Unsupported object restoration remains a documented residual limit in the
  final remediation report.

Next action:

1. Keep the regression test that rejects `모든 객체`.

### TF-STATUS-004: Export Consistency Is Explicit In The Manifest

Status: `closed`
Severity: High
Area: Rust Core dump.run manifest

Evidence:

- 2026-06-26 update: `DumpManifest` now includes `snapshot_policy`,
  `strict_export`, and `manifest_warnings`.
- 2026-06-26 update: legacy manifests default to
  `snapshot_policy = "unknown"`, `strict_export = false`, and no warnings.
- 2026-06-26 update: new single-thread exports are marked
  `connection_consistent` and strict; parallel exports are marked
  `non_consistent_parallel`, non-strict, with a warning.

Impact:

- Dump artifacts now communicate the export consistency policy instead of
  implying a shared snapshot that was not proven.

Next action:

1. Keep export consistency metadata coverage when export scheduling changes.

## Medium Priority Issues

### TF-STATUS-005: Disabled UI Features Are Labeled In Docs

Status: `closed`
Severity: Medium
Area: Docs/UI feature flags

Evidence:

- 2026-06-26 update: `SCHEDULE.md` now states that scheduled backup is
  disabled in the main UI and is retained as internal/reactivation
  documentation.
- `src/ui/main_window.py` sets `SCHEDULE_FEATURE_ENABLED = False`.
- `src/ui/main_window.py` sets `SQL_FILE_EXECUTION_FEATURE_ENABLED = False`.
- No separate public SQL file execution guide is tracked; the main context menu
  entry remains hidden by the feature flag.

Impact:

- Public schedule documentation no longer implies the feature is currently
  available in the main UI.

Next action:

1. If either feature is re-enabled, update the docs and add fresh UI/runtime
   verification evidence in the same session.

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

### TF-STATUS-007: Referenced Export/Import HTML Report Exists

Status: `closed`
Severity: Low
Area: Documentation/reporting

Evidence:

- `reports/export_import_flow_review_20260601.html` is referenced by the
  recovery design and plan.
- 2026-06-26 update: `reports/export_import_flow_review_20260601.html` exists
  and has been converted into a remediation report with verification evidence
  and residual limits.

Next action:

1. Keep the report aligned when recovery scope changes.

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

### TF-STATUS-009: Merge Import Reapplied Post-Load DDL

Status: `closed`
Severity: High
Area: Rust Core dump.import

Evidence:

- 2026-06-26 discovery: `dump_import()` applied post-load DDL unconditionally,
  including `merge` imports.
- 2026-06-26 update: `should_apply_post_load_ddl()` limits post-load DDL to
  `replace` and `recreate`; `merge` emits an explicit existing-schema skip
  phase.

Impact:

- Merge import no longer treats an existing target schema as if it had just
  been recreated.

Next action:

1. Keep the policy tests for merge/recreate behavior.

### TF-STATUS-010: Shadow Full Replacement Architecture Retired

Status: `closed`
Severity: High
Area: Rust Core dump.import

Evidence:

- The original recovery design required full replacement to load into a shadow
  schema/database, verify, then switch after verification.
- 2026-06-26 decision: current TunnelForge support is direct
  `replace`/`recreate`/`merge` import against the selected target database, not
  atomic shadow-schema replacement.
- The recovery design, recovery plan, and final remediation report now state
  this explicitly so future sessions do not implement a partial shadow helper
  without a new product decision.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/133

Impact:

- Full replacement remains non-atomic direct replacement. Strict manifest
  validation, row verification, post-load validation, classified errors, and
  import reports are the supported safety boundary.

Next action:

1. Do not reintroduce shadow replacement unless a new product decision includes
   DB-specific switch, rollback, cleanup, and worker endpoint semantics.
2. Keep UI wording aligned with direct replacement behavior.

### TF-STATUS-011: MySQL FK Charset/Collation Fidelity

Status: `closed`
Severity: High
Area: Rust Core dump.run manifest / dump.import plan

Evidence:

- The recovery design calls out MySQL charset/collation/table-option fidelity
  and treats `ERROR 3780` as a schema fidelity/import-plan validation problem.
- MySQL column inspection now captures `CHARACTER_SET_NAME` and
  `COLLATION_NAME` and preserves them in the native column type literal stored
  in the dump manifest schema.
- `dump.import` and migration post-load DDL now validate FK column
  charset/collation compatibility before applying FK DDL.
- Focused tests cover incompatible FK text collations, matching collations,
  metadata capture in MySQL inspect SQL, and MySQL-to-PostgreSQL type mapping
  with MySQL character options stripped.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/134

Impact:

- The import pipeline now classifies the `ERROR 3780` class of FK
  charset/collation mismatch as `post_load_validation_failed` before sending
  incompatible FK DDL to the target database.

Next action:

1. Keep FK fidelity regression coverage aligned with future schema metadata
   changes.
2. Track broader table-option fidelity separately if table engine/table
   collation preservation becomes a release requirement beyond FK validation.

### TF-STATUS-012: Import Cumulative Telemetry

Status: `closed`
Severity: Medium
Area: Import UI / Rust Core dump.import events

Evidence:

- GitHub issue #128 identified that Import speed and ETA could be mistaken for
  end-to-end throughput because the UI showed recent chunk speed without a
  separate cumulative baseline.
- Rust Core `dump.import` row progress events now include table-local rows and
  manifest-wide cumulative rows through `table_rows_done`,
  `table_rows_total`, `overall_rows_done`, and `overall_rows_total`.
- The Python bridge forwards the cumulative fields and calculates visible
  progress from the manifest-wide denominator when available.
- The Import dialog now displays cumulative processed rows, average speed since
  Import start, current chunk speed, and row-based ETA only while data load is
  still in progress.
- Post-load DDL emits an explicit phase event so the UI can stop implying a
  row-based ETA after data reaches 100%.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/128

Impact:

- Import progress is now anchored to the dump manifest row total instead of the
  current table/chunk alone, while recent chunk throughput remains separately
  labeled as current speed.

Next action:

1. Re-check wording with real long-running imports if additional post-load
   phases are split out later.

## Issue Tracker

| ID | Severity | Status | Area | Short Title | Next Action |
| --- | --- | --- | --- | --- | --- |
| TF-STATUS-001 | High | closed | Export/Import Recovery | Initial import intent and strictness gates | Keep regression coverage aligned with import intent changes |
| TF-STATUS-002 | High | closed | Rust Core import | Import success gated by row verification | Keep row verification/report coverage aligned with import mode changes |
| TF-STATUS-003 | High | closed | Import UI | Object restoration wording | Keep focused regression |
| TF-STATUS-004 | High | closed | Rust Core export | Export consistency explicit | Keep metadata coverage aligned with export scheduling changes |
| TF-STATUS-005 | Medium | closed | Docs/UI flags | Disabled UI features labeled | Reverify docs if feature flags change |
| TF-STATUS-006 | Medium | watch | Maintainability | Very large files | Keep fixes narrow; split later if behavior stabilizes |
| TF-STATUS-007 | Low | closed | Reporting | Referenced HTML report exists | Keep report aligned with future recovery changes |
| TF-STATUS-008 | Low | watch | macOS | Final real-Mac validation pending | Require evidence bundle before production-ready claim |
| TF-STATUS-009 | High | closed | Rust Core import | Merge import post-load DDL policy | Keep merge/recreate policy tests |
| TF-STATUS-010 | High | closed | Rust Core import | Shadow replacement retired; direct replacement documented | Keep UI/docs aligned |
| TF-STATUS-011 | High | closed | Rust Core schema fidelity | MySQL FK charset/collation fidelity | Keep FK fidelity regression coverage |
| TF-STATUS-012 | Medium | closed | Import UI telemetry | Cumulative Import rows/s and ETA | Re-check wording with real long-running imports |

## Recommended Execution Order

1. Keep macOS real-device validation tracked separately.

## Session Log

| Date | Session Summary | Files Touched | Verification |
| --- | --- | --- | --- |
| 2026-06-26 | Created canonical status inventory after full repo survey. | `docs/current_status.md` | `pytest -q`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `compileall`; `git diff --check`; `service.hello` |
| 2026-06-26 | Added Python import payload forwarding for `timezone_sql` and `strict_manifest`; removed import UI `모든 객체` overpromise; added focused regression tests. | `src/exporters/rust_dump_exporter.py`, `src/ui/dialogs/db_dialogs.py`, `tests/test_rust_dump_exporter.py`, `tests/test_db_dialogs.py`, `docs/current_status.md` | `python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `git diff --check` |
| 2026-06-26 | Added Rust validation/application for dump import `timezone_sql`; arbitrary SQL and multi-statement payloads are rejected before DB connection. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_session_time_zone_only --lib`; `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q` |
| 2026-06-26 | Added strict manifest classification before dump import target mutation and preserved classified core errors through Python import messages. | `migration_core/src/lib.rs`, `tests/test_rust_dump_exporter.py`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added dump import row-count success gate and `_tunnelforge_import_report.json` success artifact. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `cargo test --manifest-path migration_core\Cargo.toml write_dump_import_report_creates_json_file --lib` |
| 2026-06-26 | Added dump manifest consistency metadata for strict and non-strict export paths. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added merge import post-load DDL skip policy, fixed English translation for import UI wording, and created the final remediation report. | `migration_core/src/lib.rs`, `src/core/i18n.py`, `reports/export_import_flow_review_20260601.html`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `.venv\Scripts\python -m pytest -q`; `compileall`; `git diff --check` |
| 2026-06-26 | Marked scheduled backup documentation as disabled/internal while the main UI feature flag remains off. | `SCHEDULE.md`, `docs/current_status.md` | `rg -n "SCHEDULE_FEATURE_ENABLED|SQL_FILE_EXECUTION_FEATURE_ENABLED|스케줄" src docs SCHEDULE.md` |
| 2026-06-26 | Re-audited recovery design residuals after user challenge; added explicit open tracking for shadow replacement and MySQL schema fidelity gaps. | `docs/current_status.md` | `rg -n "shadow|ERROR 3780|charset|collation" docs/superpowers/specs/2026-06-01-export-import-recovery-design.md docs/superpowers/plans/2026-06-01-export-import-recovery.md reports/export_import_flow_review_20260601.html migration_core/src/lib.rs` |
| 2026-06-26 | Created GitHub issues for remaining recovery gaps. | `docs/current_status.md` | `gh issue create` created #133 and #134 |
| 2026-06-26 | Added MySQL FK charset/collation fidelity capture and post-load validation for GitHub #134. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `.venv\Scripts\python -m pytest tests\test_rust_dump_exporter.py -q -k "classified_core_error"`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Explicitly retired shadow full replacement as a current guarantee and documented direct replacement as the supported import architecture for GitHub #133. | `docs/superpowers/specs/2026-06-01-export-import-recovery-design.md`, `docs/superpowers/plans/2026-06-01-export-import-recovery.md`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | `rg -n "shadow|direct replacement|atomic" docs/superpowers/specs/2026-06-01-export-import-recovery-design.md docs/superpowers/plans/2026-06-01-export-import-recovery.md docs/current_status.md reports/export_import_flow_review_20260601.html`; `git diff --check` |
| 2026-06-26 | Closed resolved import issues #120 and #123; added classified table/operation context for direct replace DDL failures supporting #119. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | `cargo test --manifest-path migration_core\Cargo.toml`; focused RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml dump_import_ddl_error_includes_classification_table_and_operation --lib` |
| 2026-06-26 | Fixed post-load DDL ordering so all secondary/unique indexes are applied before any foreign keys, addressing GitHub #127. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml post_data_ddl_applies_all_indexes_before_any_foreign_keys --lib`; `cargo test --manifest-path migration_core\Cargo.toml` |
| 2026-06-26 | Added final target row-count verification for direct replace/recreate imports, addressing GitHub #131 while preserving merge semantics. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml import_target_row_verification --lib`; `cargo test --manifest-path migration_core\Cargo.toml` |
| 2026-06-26 | Classified post-load DDL execution failures with the failing SQL statement for diagnosis of errors such as GitHub #126. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_errors_include_classification_and_sql_context --lib`; `cargo test --manifest-path migration_core\Cargo.toml` |
| 2026-06-26 | Added cumulative Import telemetry for GitHub #128: Rust row events now carry table-local and manifest-wide row counts, Python forwards them, and the UI separates average speed, current speed, ETA, and post-load phase text. | `migration_core/src/lib.rs`, `src/exporters/rust_dump_exporter.py`, `src/ui/dialogs/db_dialogs.py`, `tests/test_db_dialogs.py`, `tests/test_rust_dump_exporter.py`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `pytest tests/test_db_dialogs.py::test_format_import_row_labels_reports_cumulative_average_current_and_eta tests/test_db_dialogs.py::test_format_import_row_labels_stops_row_eta_during_post_load_phase tests/test_rust_dump_exporter.py::TestRustDumpImporter::test_import_row_progress_forwards_cumulative_totals_to_detail_callback`; `cargo test --manifest-path migration_core\Cargo.toml dump_import_row_progress_event_reports_table_and_overall_rows`; final: `pytest tests/test_db_dialogs.py tests/test_rust_dump_exporter.py`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `pytest -q`; `python -m compileall -q main.py src tests`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
