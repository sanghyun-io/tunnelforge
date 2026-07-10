# TunnelForge Current Status

Last reviewed: 2026-07-10

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

TunnelForge is in a strong build/test state. The active architecture baseline
is Rust Core ownership of DB operations through `tunnelforge-core`, with
Python/PyQt responsible for UI, orchestration, signals, and dialogs.

The `2.3.1` release candidate now contains the completed release-trust scope:
GitHub Release asset `digest` verification prevents unverified downloaded
packages from launching, unknown-environment confirmation protects dangerous
operations when tunnel metadata is absent or unclassified, `python-regression`
preserves the full Python suite in CI, and the bilingual Schedule correction
states that scheduled backups and queries remain disabled. TF-STATUS-079,
TF-STATUS-080, and TF-STATUS-082 are closed by this release-candidate
verification. TF-STATUS-081 and TF-STATUS-083 are
`fixed_pending_full_verify` because RC merge/tag and stable required-check
promotion are external follow-ups; TF-STATUS-008 and TF-STATUS-078 remain open.
The release-candidate full Python suite passed at 1870 passed / 4 warnings in
60.08s with exit 0; the matching Rust Core gate, Cargo test/build, version-sync,
and diff checks also passed in this session.

Clean Code Round 3 completed on 2026-07-09: the remaining UI/dialog/main-window
refactor work packages WP-3.1 through WP-3.8 were integrated as
behavior-preserving commits. A red-review follow-up restored compatibility for
legacy migration worker constructor kwargs, `CleanupWorker(dry_run=False)`
fail-closed behavior, and Fix Wizard dialog module re-exports including
`BatchOptionDialog`. The SECURE follow-up removed frozen-runtime core helper
`cwd`/implicit `PATH` lookup and hardened schema-derived auto-save filenames
for analysis and rollback files. The integrated main tree passed the Rust Core
regression gate, a whole-tree `MySQLConnector` allowlist scan, Round 3 focused
tests at 491 passed, and the post-strategy-review full Python suite at 1827
passed / 6 warnings.

A role-specialized strategy/security review on 2026-07-10 confirmed that the
next work is release trust rather than another broad refactor. Downloaded update
packages are executed without an application-level hash/signature verification
gate, unset environments allow dangerous SQL without confirmation, current
`main` contains unreleased post-release commits while still declaring the
published version, README advertises scheduled backups while the UI feature
flag is disabled, and branch protection requires only `version-gate`. These
are tracked as TF-STATUS-079 through TF-STATUS-083.

GitHub #170 remains open for issue hygiene only: its reported MySQL ERROR 3780
import path was fixed by PR #171 / commit `a4c7a06`, that fix is contained in
current `main`, and release tags `v2.1.8` through `v2.3.0` contain it. Confirm
the merged fix with the reporter and close the issue unless the failure can be
reproduced on a containing release; it is not remaining Clean Code Round 3
implementation work.

Open GitHub issue #116 remains external. Its current final gate needs both
current-HEAD manual workflow evidence and the real-Mac report before closure.
GitHub #142 is fixed: the legacy Python
Auto-Fix Wizard mutation path is now fail-closed from the user-visible worker
path, legacy Python Auto-Fix Wizard mutations are no longer executable from
that path, and Legacy Auto-Fix Wizard is dry-run/manual SQL only. GitHub issues
#137 through #141 closed the current One-Click
readiness sequence: dry-run preview, limited `deprecated_engine ->
engine_innodb` real execution, charset/collation supplied contract execution,
PyQt-triggered charset contract derivation, and display-only
`int_display_width` skip policy. No repo-side One-Click follow-up issue is
currently open; track each additional automatic-fix class as a separate issue
before implementation.

On 2026-06-27, the remaining repo-side #116 handoff drift found in the next
issue analysis was closed: macOS artifact download defaults now use the PR head
before merge, or current merged main HEAD after PR #117 is merged, matching the
final gate/report SHA policy. #116 itself remains open only for real operator
Mac validation evidence.

The scheduled-backup guide is also reconfirmed as an internal/reactivation
memo while `SCHEDULE_FEATURE_ENABLED = False`; it must not read like current
public UI instructions until the feature flag is intentionally re-enabled and
runtime evidence is refreshed.

One-Click readiness wording is reconfirmed against the current limited
real-execution gate: the app supports only backup-confirmed
`deprecated_engine -> engine_innodb` non-dry-run execution, while broad
production automatic remediation and production charset/collation execution
remain unsupported.

Current main next-issue re-audit on 2026-06-27 initially confirmed only #116
was open and found no Rust Core baseline violation in legacy connector names:
`MySQLConnector`/`PostgresConnector` route through
`DbCoreFacade`/`RustDbConnection`, hidden schedule SQL execution uses the Rust
connector shim when enabled, and SQL editor query execution also routes through
the Rust connector shim. A later focused audit found a different repo-side
baseline gap: the legacy Auto-Fix Wizard mutation policy is still owned by
Python, now tracked as GitHub #142 / TF-STATUS-040.

That Legacy Auto-Fix Wizard mutation path was fixed later on 2026-06-27:
`FixWizardDialog.ExecutionPage` now starts `FixWizardWorker` with
`dry_run=True`, the worker rejects `dry_run=False`, and the UI text presents
the page as SQL/Dry-run confirmation instead of DB execution.

GitHub #143 is fixed as the deeper follow-up: the legacy Auto-Fix core
mutation APIs now fail-close when `dry_run=False` is requested, and the
legacy Auto-Fix core mutation APIs are no longer executable in Python mutation
mode.
`BatchFixExecutor.execute_batch` and
`FKSafeCharsetChanger.execute_safe_charset_change` reject Python-owned DB
mutation mode before session state or execution hooks are touched, and
`BatchFixExecutor._execute_single` is also fail-closed if called directly,
while dry-run/SQL generation remains available. Direct
`cursor.execute`/`commit`/`rollback` mutation calls were removed from
`src/core/migration_fix_wizard.py`.

GitHub #144 is fixed as the next Rust Core baseline follow-up: the legacy
MigrationAnalyzer cleanup mutations now fail-close when `dry_run=False` is
requested, and the migration analyzer dialog no longer offers legacy
Python-owned actual cleanup execution. `MigrationAnalyzer.execute_cleanup`
rejects non-dry-run mutation mode before cursor/session/commit/rollback work,
while Dry-Run and SQL preview remain available.

GitHub #145 is fixed as the worker-level follow-up: legacy CleanupWorker
actual cleanup mode now fails closed at construction time. `CleanupWorker(...,
dry_run=False)` is rejected before a thread can emit misleading `[실행]`
progress or call the analyzer path, while Dry-run cleanup worker construction
remains available.

GitHub #146 is fixed as the connector-surface follow-up: the unused legacy
MySQLConnector execute_many mutation helper now fails closed before cursor or
commit work. `MySQLConnector.execute_many` no longer exposes a dormant Python
batch mutation helper, while existing read/query helper behavior is unchanged.

Post-#146 next issue analysis on 2026-06-27 reconfirmed #116 was the only open
GitHub issue. The normal repository-side #116 gate passed, and the then-current
final-gate blockers were external validation evidence rather than a new
repo-side implementation issue. The older manual-workflow portion of that
finding is superseded by later current-head workflow refreshes on #116; the
current blocker is missing real-Mac report evidence under `build/`.

GitHub #147 is fixed as the release-readiness follow-up: post-release version
drift after `v2.1.6` is resolved by bumping the next unreleased source version
to `2.1.7` across `src/version.py`, `pyproject.toml`, and
`installer/TunnelForge.iss`.

GitHub #148 is fixed as the release-publication follow-up: tag `v2.1.7` was
created from current `main` commit `fa22306`, Build and Release workflow run
`28255274238` completed successfully, and GitHub release `v2.1.7` was
published with `TunnelForge-Setup-2.1.7.exe`, `TunnelForge-WebSetup.exe`,
`TunnelForge-macOS-2.1.7-arm64.dmg`,
`TunnelForge-macOS-2.1.7-arm64.zip`,
`TunnelForge-macOS-2.1.7-x86_64.dmg`,
`TunnelForge-macOS-2.1.7-x86_64.zip`, and checksum assets.

Post-#148 next issue analysis on 2026-06-27 reconfirmed #116 was the only open
GitHub issue. The normal repository-side #116 gate passed, and the then-current
final-gate blockers were external validation evidence rather than repo-side
implementation work. The older manual-workflow portion of that finding is
superseded by later current-head workflow refreshes on #116; the current
blocker is missing real-Mac report evidence under `build/`.

GitHub #149 is fixed as the next release-readiness follow-up: post-v2.1.7
version drift after release-tracking commits was resolved by bumping the next
unreleased source version to `2.1.8` across `src/version.py`,
`pyproject.toml`, and `installer/TunnelForge.iss`.

GitHub #150 is fixed as a Rust Core baseline hardening follow-up: the unused
`RustDbCursor.executemany` Python-side batch helper now fails closed before
any query/facade call. Explicit single-query Rust Core execution paths remain
unchanged, and batch DB operations must be modeled as explicit Rust Core
commands.

GitHub #151 is fixed as a current-status handoff cleanup: stale current-tense
`1830 passed, 5 warnings` full-suite wording from TF-STATUS-049 is now
superseded by later full-suite evidence from TF-STATUS-050 / TF-STATUS-051.

Post-#151 main merge and next issue analysis on 2026-06-27 reconfirmed that
main was aligned with origin/main before that status update, the status update
was pushed to origin/main, #116 was still the only open GitHub issue, and the
normal repository-side #116 gate passed. The then-current final-gate blockers
were external validation evidence rather than repo-side implementation work.
The older manual-workflow portion of that finding is superseded by later
current-head workflow refreshes on #116; the current blocker is missing
real-Mac report evidence under `build/`.

GitHub #152 is fixed as the post-#151 full-suite evidence refresh: after adding
post-#151 current-status coverage, `pytest -q` reported `1839 passed, 5
warnings`; that count is now superseded by TF-STATUS-057 full-suite evidence.

GitHub #153 is fixed as a Rust Core DML affected-row reporting follow-up:
Rust Core `query.execute` now returns `rows_affected` for non-row-returning
statements, Python `DbCoreFacade` preserves that metadata, and
`RustDbCursor.rowcount` uses it for DML. Scheduled SQL and SQL editor DML
reporting can now show real affected-row counts instead of the previous
empty-row fallback count.

GitHub #154 is fixed as the call-local affected-row metadata follow-up:
`DbCoreFacade.execute_query_result` and `execute_on_connection_result` now
return rows plus `rows_affected` together, and `RustDbCursor.rowcount` consumes
that per-call result instead of shared facade state. This prevents concurrent
cursor calls on the shared Rust Core facade from mixing DML rowcount metadata.

GitHub #155 is fixed as the SQL statement parser mismatch follow-up:
`src/core/sql_statement_parser.py` now owns the shared robust parser for SQL
file execution, SQL Editor execute-all/current-query, and hidden scheduled SQL.
`find_sql_statement_at_position` uses parser ranges so SQL Editor current-query
execution returns a whole statement when the cursor is inside comments,
PostgreSQL dollar quote bodies, quoted identifiers, or MySQL DELIMITER scripts.

GitHub #156 is fixed as a SQL dollar quote helper guard follow-up:
`read_dollar_quote` now returns an empty marker for empty SQL text and
out-of-range start offsets instead of raising `IndexError` or inspecting a
negative Python index. The compatibility wrapper
`SQLExecutionWorker._read_dollar_quote` now inherits the same fail-closed
behavior.

GitHub #157 is fixed as a One-Click readiness handoff cleanup:
`docs/oneclick_readiness.md` no longer labels the completed One-Click guidance
as `Recommended next repo-side change`. The section is now standing policy
for future One-Click automatic-fix expansion and explicitly states that no
repo-side One-Click follow-up issue is currently open.

GitHub #158 is fixed as a SQL dollar quote helper None input follow-up:
`read_dollar_quote(None, 0)` and
`SQLExecutionWorker._read_dollar_quote(None, 0)` now return an empty marker
instead of raising `TypeError`, matching the parser's existing fail-closed
empty-input behavior.

Post-#156 main merge and next issue analysis on 2026-06-27 reconfirmed that
`main` was already aligned with `origin/main`, #116 was still the only open
GitHub issue, and the normal repository-side #116 gate passed. The then-current
final-gate blockers were external validation evidence rather than repo-side
implementation work. The older manual-workflow portion of that finding is
superseded by later current-head workflow refreshes on #116; the current
blocker is missing real-Mac report evidence under `build/`.

The current full Python suite count was refreshed again on 2026-06-27 after
the latest status update regression coverage was added.

GitHub #160 is fixed: partial Export FK parent auto-inclusion now resolves
transitive parent tables through Rust Core-owned schema inspection
(`schema.inspect`) instead of constructing a Python `MySQLConnector` in
`RustDumpExporter.export_tables`.

GitHub #161 is fixed: PostgreSQL Export/Import now preserves the PostgreSQL
engine from `PostgresConnector` through `RustDumpConfig` into Rust Core
`dump.run` and `dump.import` endpoints instead of falling back to MySQL.

GitHub #162 is fixed as the PostgreSQL Import timezone follow-up: the Import
dialog no longer runs MySQL `mysql.time_zone_name` auto-detection or sends
MySQL `SET SESSION time_zone` correction SQL for PostgreSQL dump imports.
PostgreSQL default auto mode now leaves timezone SQL unset, while forced KST
and UTC options use PostgreSQL `SET TIME ZONE` syntax.

GitHub #163 is fixed as the Rust Core boundary follow-up: `dump.import`
timezone validation now accepts the safe PostgreSQL `SET TIME ZONE` form in
addition to the existing MySQL `SET SESSION time_zone` form, while still
rejecting multi-statement SQL, comments, global timezone mutation, and unsafe
timezone literals.

GitHub #164 is fixed as the PostgreSQL dump wrapper API follow-up: the
module-level `export_schema`, `export_tables`, and `import_dump` convenience
wrappers now accept an optional `engine` parameter, default to MySQL for
backward compatibility, and preserve `engine="postgresql"` into
`RustDumpConfig` for Rust Core endpoints.

GitHub #165 is fixed as the hidden scheduled backup follow-up:
`BackupScheduler._execute_backup` now preserves PostgreSQL tunnel engine
metadata into `RustDumpConfig`, matching the engine-aware scheduled SQL path.
Because `SCHEDULE_FEATURE_ENABLED = False`, this was a reactivation/internal
path issue rather than a current public UI regression.

GitHub #166 is fixed as the next hidden scheduled backup follow-up:
`BackupScheduler._execute_backup` now accepts the real
`TunnelEngine.get_connection_info()` `(host, port)` tuple shape as well as
dict-shaped test doubles, and resolves credentials through
`config_manager.get_tunnel_credentials(...)` or tunnel config fallbacks before
constructing `RustDumpConfig`.

Post-#166 next issue re-audit on 2026-06-27 reconfirmed #116 was the only open
GitHub issue and the normal repository-side #116 gate passed. The then-current
final-gate blockers were external validation evidence rather than repo-side
implementation work. The older manual-workflow portion of that finding is
superseded by later current-head workflow refreshes on #116; the current
blocker is missing real-Mac report evidence under `build/`. Rust Core baseline
and stale handoff scans found no new repo-side implementation issue.

GitHub #167 is fixed as the #116 current-head workflow evidence handoff
follow-up: `docs/current_status.md` no longer treats an exact manual macOS
workflow run ID or SHA as durable current-head evidence. Exact current-head
manual workflow evidence is tracked on GitHub #116 comments after status-only
commits, and `scripts\check-macos-support-gate.py --final` is the authoritative
check that the latest successful manual `macOS App Validation`
`workflow_dispatch` run matches current `main`. GitHub #116 remains external
because the final gate still fails only for missing real operator Mac validation
report evidence under `build/`.

GitHub #116 manual macOS workflow evidence was refreshed during this session:
a manual `macOS App Validation` workflow_dispatch run passed for the
then-current main HEAD, including both `arm64` and `x86_64` jobs. That evidence
is historical in this document because status-only commits advance `main`;
rerun the manual workflow after such commits and record the exact current-head
run on #116.

GitHub #168 is fixed as the current focused final-gate row cleanup: the
current focused final-gate row now fails only for missing real-Mac report,
matching the latest `scripts\check-macos-support-gate.py --final` output after
current-head manual workflow evidence was refreshed on #116.

GitHub #169 is fixed as the current-status Summary cleanup: superseded
missing-manual-workflow wording from older re-audit paragraphs is no longer
presented as current Summary state. The Summary now keeps the current #116
blocker focused on missing real operator Mac validation report evidence.

Post-#169 next issue re-audit on 2026-06-27 reconfirmed GitHub #116 is still
the only open issue. Rust Core boundary and stale handoff scans confirmed that
no new repo-side implementation issue was found: legacy-shaped connector calls
still route through Rust Core shims, the only external command hit was live
evidence container seeding, and current open work remains external real-Mac
validation report evidence. Current-head manual workflow evidence remains
tracked on #116 comments and by `scripts\check-macos-support-gate.py --final`,
not as a durable exact run ID in this Summary.

GitHub #116 final validation tooling was rechecked on 2026-06-27: the macOS
manual validation/report scripts still parse cleanly, macOS focused tests still
pass at 53 passed, the normal #116 repository-side gate passes, and the final
gate accepts the latest current-head manual workflow proof while failing only
for the missing real-Mac report under `build/`. No additional repo-side tooling
issue was found.

Post-#142 next issue analysis on 2026-06-27 found #116 was still the only open
GitHub issue and the normal repository-side macOS support gate passed. The
then-current final-gate blockers were external validation evidence rather than
a new repo-side implementation issue. The older manual-workflow portion of that
finding is superseded by later current-head workflow refreshes on #116; the
current blocker is missing real-Mac report evidence under `build/`.

Rust Core Export/Import context-menu wording was realigned on 2026-06-27 so
the visible tunnel actions and handlers match the Rust Core implementation
instead of legacy shell-branded labels.

One-Click fallback dry-run tooltip wording was also cleaned up on 2026-06-27:
if real execution is disabled in a future build, the dialog now explains that
real execution is disabled in this build instead of pointing at the already
closed GitHub #138 gate.

One-Click module scope wording now matches the current implementation: Rust DB
Core owns the workflow, dry-run is the default, and real execution is limited
to backup-confirmed validated scopes.

Windows installer examples in `BUILD.md` now avoid the stale `1.0.0` sample
version and use `{version}` / `{#MyAppVersion}` placeholders aligned with the
release version sync path.

The next #116 repo-side analysis found and closed one final-gate mismatch:
after PR #117 has merged, the manual workflow_dispatch artifact run now follows
the same head policy as the final report and artifact download path: PR head
before merge, current merged main HEAD after merge.

A post-merge next-issue external re-audit on 2026-06-27 reconfirmed that
`main` is aligned with `origin/main`, #116 remained external, the full #116
repository-side gate passed, and SQL editor query execution also routes through
the Rust connector shim. The follow-up baseline scan created GitHub #142 after
confirming the separate legacy Python Auto-Fix Wizard mutation path.

## Current Baseline Verification

Commands run locally. The `2.3.1` release candidate verification refreshes the
current Python, Rust Core, release-build, version-sync, and diff evidence;
the latest status update preserves earlier broad evidence rows where their
commands were not rerun, including the historical `Full-suite count refreshed on 2026-06-27` baseline.

| Check | Result |
| --- | --- |
| `git status --short --branch` | Tracked Round 3 commits integrated on `main`; only status/handoff scratch files remain outside the integration commits before this status update |
| `pytest -q` | PASS, 1870 passed, 4 warnings, 60.08s, exit 0 |
| Round 3 focused pytest suite | PASS, 491 passed, 2 warnings |
| `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS, 1.4s, exit 0 |
| whole-tree `MySQLConnector` allowlist scan | PASS, 22 product imports and no missing allowlist entries |
| `cargo test --manifest-path migration_core\Cargo.toml` | PASS, 216 lib, 2 JSONL CLI, 9 live roundtrip, and 2 stress tests passed; 1 stress test ignored, 4.1s, exit 0 |
| `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS, 36.61s, exit 0 |
| `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q` | PASS, 1 passed in 0.08s, exit 0 |
| `python -m compileall -q main.py src tests scripts` | PASS |
| `git diff --check` | PASS, 0.5s, exit 0 |
| `tunnelforge-core service.hello` | PASS, reports `dump.run`, `dump.import`, migration commands, and `oneclick.*` commands |
| `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip -- --nocapture` | PASS, 6 live container MySQL/PostgreSQL smoke tests |
| `pytest tests\test_live_ui_migration_capture.py tests\test_live_ui_migration_evidence.py -q` | PASS, capture and validator tests |
| `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS |
| `python scripts\check-macos-support-gate.py --skip-github` | PASS |
| `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS, 53 passed |
| `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL, missing current-HEAD manual workflow evidence and real-Mac report |
| GitHub required status checks | `version-gate` only |

Historical Round 3 baseline snapshot (preserved for audit):

    | `pytest -q` | PASS, 1827 passed, 6 warnings |

Version references are aligned at `2.3.1` across:

- `src/version.py`
- `pyproject.toml`
- `installer/TunnelForge.iss`

## Focused Verification On 2026-06-27

Commands run locally:

| Check | Result |
| --- | --- |
| `pytest -q` | PASS, 1876 passed, 5 warnings |
| `python scripts\check-macos-support-gate.py --skip-github` | PASS |
| `python scripts\check-macos-support-gate.py` | PASS |
| `pytest tests\test_build_docs.py tests\test_current_status_docs.py::test_current_status_records_build_doc_installer_version_cleanup -q` | RED then PASS |
| `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_module_docstring_matches_limited_rust_core_scope tests\test_current_status_docs.py::test_current_status_records_oneclick_module_scope_docstring_cleanup -q` | RED then PASS |
| `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_disabled_real_execution_tooltip_does_not_reference_closed_138 tests\test_current_status_docs.py::test_current_status_records_oneclick_fallback_dry_run_tooltip_cleanup -q` | RED then PASS |
| `pytest tests\test_main_window_export_import_labels.py -q` | PASS |
| `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_manual_workflow_after_pr_merge -q` | RED then PASS |
| `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_uses_local_head_after_pr_merge -q` | RED then PASS |
| `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS, 53 passed |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_merge_next_issue_external_reaudit -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_python_auto_fix_wizard_issue -q` | RED then PASS |
| `pytest tests\test_fix_wizard_dialog.py -q` | RED then PASS, 2 passed |
| `pytest tests\test_migration_fix_wizard.py -q` | RED then PASS, 88 passed |
| `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q` | PASS, 20 passed |
| `pytest tests\test_migration_analyzer.py::TestExecuteCleanup::test_actual_cleanup_rejects_legacy_python_mutation_mode tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q` | RED then PASS |
| `pytest tests\test_migration_analyzer.py::TestExecuteCleanup tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q` | PASS, 3 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_migration_analyzer_cleanup_issue -q` | RED then PASS |
| `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_migration_analyzer.py::TestExecuteCleanup tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled tests\test_current_status_docs.py::test_current_status_tracks_legacy_migration_analyzer_cleanup_issue tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q` | PASS, 6 passed, 2 warnings |
| `pytest tests\test_migration_worker.py -q` | RED then PASS, 2 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_cleanup_worker_issue -q` | RED then PASS |
| `pytest tests\test_db_connector.py::TestMySQLConnector::test_execute_many_rejects_legacy_python_mutation_helper -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_execute_many_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_146_next_issue_analysis -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_release_version_drift_issue -q` | RED then PASS |
| `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q` | PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_v217_release_publication_issue -q` | RED then PASS |
| `gh run view 28255274238 --json status,conclusion,url` | PASS, Build and Release workflow completed successfully |
| `gh release view v2.1.7 --json tagName,name,url,assets,publishedAt,targetCommitish,isDraft,isPrerelease` | PASS, release `v2.1.7` published with Windows and macOS assets |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_148_next_issue_analysis -q` | RED then PASS |
| `git status --short --branch` | `## main...origin/main`, no local changes before #116 re-analysis |
| `gh issue list --state open --limit 20` | PASS, only #116 open |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_v217_version_drift_issue -q` | RED then PASS |
| `python scripts\bump_version.py --bump-type patch` | PASS, bumped `2.1.7` to `2.1.8` |
| `pytest tests\test_db_core_service.py::test_rust_db_cursor_executemany_rejects_python_batch_helper -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_rust_db_cursor_executemany_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_does_not_describe_stale_full_pytest_count_as_current -q` | RED then PASS |
| `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL, missing real-Mac report only |
| `bash -n scripts/macos-download-validation-artifacts.sh scripts/macos-manual-validation-report.sh` | PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_151_next_issue_analysis -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py -q` | PASS, 53 passed |
| `python -m compileall -q src\core\i18n.py src\ui\dialogs\fix_wizard_dialog.py src\ui\workers\fix_wizard_worker.py tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py` | PASS |
| `git diff --check` | PASS |
| `gh issue create --title "Unify SQL statement parsing across SQL Editor and execution paths" ...` | PASS, created #155 |
| direct parser comparison between `SQLEditorDialog._split_queries` and `SQLExecutionWorker._parse_sql_statements` | PASS, reproduced SQL Editor over-splitting comments, PostgreSQL dollar quote bodies, and MySQL `DELIMITER` scripts |
| `pytest tests\test_sql_editor_dialog.py::test_split_queries_preserves_comments_dollar_quotes_and_delimiters tests\test_sql_editor_dialog.py::test_get_query_at_cursor_uses_statement_parser_ranges -q` | RED then PASS |
| `pytest tests\test_scheduler.py::TestBackupScheduler::test_parse_sql_queries_preserves_comments_dollar_quotes_and_delimiters -q` | RED then PASS |
| `pytest tests\test_sql_editor_dialog.py tests\test_scheduler.py tests\test_sql_execution_worker.py -q` | PASS, 71 passed, 2 warnings |
| `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_out_of_range_starts -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_156_next_issue_analysis -q` | RED then PASS |
| `gh issue list --state open --limit 30 --json number,title,labels,url,updatedAt` | PASS, only #116 open |
| `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_does_not_present_closed_issues_as_current_tracking -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_oneclick_next_action_wording_issue -q` | RED then PASS |
| `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_none_sql_text -q` | RED then PASS |
| `pytest tests\test_sql_execution_worker.py tests\test_sql_editor_dialog.py tests\test_scheduler.py -q` | PASS, 73 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_dollar_quote_none_input_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_baseline_provenance_uses_latest_status_update -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_tables_resolves_fk_parents_through_rust_schema_inspect -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py -q` | PASS, 37 passed, 2 warnings |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_partial_export_fk_parent_rust_inspect_issue -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py::TestRustDumpConfig::test_config_preserves_postgresql_engine tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_full_schema_preserves_postgresql_engine_in_rust_payload tests\test_rust_dump_exporter.py::TestRustDumpImporter::test_import_dump_preserves_postgresql_engine_in_rust_payload -q` | RED then PASS |
| `pytest tests\test_db_dialogs.py::test_preselected_export_tunnel_uses_postgres_connector_for_postgresql tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_rust_dump_engine_issue -q` | RED then PASS |
| `pytest tests\test_db_dialogs.py -q -k "postgresql_import_auto_timezone or postgresql_import_forced_kst"` | RED then PASS |
| `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_db_dialogs.py -q -k "direct_hardcoded or postgresql_import_auto_timezone or postgresql_import_forced_kst"` | PASS, 3 passed |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_issue -q` | RED then PASS |
| `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_mysql_and_postgresql_timezone_forms --lib` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_core_validation_issue -q` | RED then PASS |
| `pytest tests\test_rust_dump_exporter.py -q -k "wrapper_preserves_postgresql_engine"` | RED then PASS, 3 passed |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_dump_wrapper_engine_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_postgresql_engine_issue -q` | RED then PASS |
| `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_preserves_postgresql_engine_for_rust_dump -q` | RED then PASS |
| `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_accepts_tuple_connection_info_for_rust_dump -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_tuple_connection_issue -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_166_next_issue_reaudit -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_manual_macos_workflow_evidence -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_tracks_non_self_stale_macos_workflow_evidence_policy -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_focused_final_gate_reason_matches_current_workflow_evidence -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_summary_does_not_keep_superseded_missing_manual_workflow_wording -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_post_169_next_issue_reaudit -q` | RED then PASS |
| `pytest tests\test_current_status_docs.py::test_current_status_records_macos_final_validation_tooling_recheck -q` | RED then PASS |

## Verification Log

| Date | Scope | Command | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-07-10 | Task 6 review follow-up: historical-versus-RC status evidence | RED then GREEN: `pytest tests\test_current_status_docs.py tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q` | PASS, 60 passed in 0.25s | RED had 2 failures in 0.44s: the Round 3 `1827 passed / 6 warnings` baseline snapshot was no longer asserted/present, and Session Log had two delimiter rows. Restored the historical assertion, split `1870 passed / 4 warnings` plus Rust evidence into a dedicated RC test, preserved both records, and removed the duplicate delimiter. |
| 2026-07-10 | `2.3.1` release-candidate status and version finalization | RED: `pytest tests\test_current_status_docs.py tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q`; GREEN: same focused command after `.venv\Scripts\python.exe scripts\bump_version.py --bump-type patch`; `$env:PYTHONUTF8='1'; $env:QT_QPA_PLATFORM='offscreen'; pytest -q`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; version-sync pytest; `git diff --check` | PASS, all exit 0 | RED: 1 failed, 57 passed in 0.43s because source was `2.3.0`; bump emitted `new_version=2.3.1`; GREEN: 58 passed in 0.26s. Full Python: 1870 passed, 4 warnings in 60.08s. Rust gate: 1.4s; Cargo test: 216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored in 4.1s; release build: 36.61s; version-sync: 1 passed in 0.08s; diff check: 0.5s. The handoff records GitHub Release asset `digest` verification, unknown-environment confirmation, `python-regression`, the bilingual Schedule correction, and the `2.3.1` release candidate. |
| 2026-07-10 | Role-specialized strategy, release, and security review | six role-specific read-only repository reviews plus cross-critique; `python scripts\check-macos-support-gate.py --final`; `git rev-list --left-right --count v2.3.0...HEAD`; `gh api repos/sanghyun-io/tunnelforge/branches/main/protection/required_status_checks`; focused source tracing for updater execution and ProductionGuard; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `git diff --check` | PASS with expected macOS final-gate failure | Confirmed TF-STATUS-079 through TF-STATUS-083 and refreshed TF-STATUS-008. Current-status tests passed at 56 passed; full Python suite passed at 1827 passed / 6 warnings. The final macOS gate fails only for missing current-HEAD manual workflow evidence and the real-Mac report. |
| 2026-07-10 | Round 3 completion and open-issue reconciliation | `git status --short --branch`; `git rev-list --left-right --count origin/main...main`; `gh issue list --state open --limit 30 --json number,title,updatedAt,url`; `gh issue view 170 --json ...`; `git branch --contains a4c7a06`; `git merge-base --is-ancestor a4c7a06 HEAD`; `gh pr view 171 --json ...`; `git tag --contains a4c7a06`; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `git diff --check` | PASS | Round 3 remains complete and pushed at `09ab060`. GitHub #170 is still open, but PR #171 fixed its ERROR 3780 path, the fix is in current `main`, and release tags from `v2.1.8` through `v2.3.0` contain it. Remaining #170 work is issue confirmation/closure, not implementation. Current-status tests passed at 55 passed; full Python suite passed at 1826 passed / 6 warnings. |
| 2026-07-09 | Clean Code Round 3 UI/dialog/main-window integration | `python -m py_compile` on all Round 3 production Python files; `pytest` focused Round 3 suite; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; custom whole-tree `MySQLConnector` allowlist scan; `git diff --check HEAD~8..HEAD`; `pytest -q` | PASS | Integrated WP-3.1 through WP-3.8 as behavior-preserving commits. Focused Round 3 tests passed at 491 passed / 2 warnings, Rust Core regression gate passed, allowlist scan found 22 product imports with no missing entries, and full Python suite passed at 1819 passed / 4 warnings. |
| 2026-07-09 | Clean Code Round 3 red-review follow-up | RED/GREEN: migration worker constructor compatibility and cleanup dry-run rejection tests; Fix Wizard dialog re-export regression test; `python -m py_compile src/ui/workers/migration_worker.py src/ui/dialogs/fix_wizard_dialog.py tests/test_migration_worker.py tests/test_fix_wizard_dialog.py`; `pytest tests/test_migration_worker.py tests/test_fix_wizard_dialog.py tests/test_migration_fix_wizard.py tests/test_fix_wizard_sql_helpers.py -q`; `pytest -q` | PASS | Red-review found behavior-preserving compatibility regressions in `MigrationAnalyzerWorker` legacy `check_*` kwargs, `CleanupWorker(dry_run=False)` fail-closed semantics, and `fix_wizard_dialog` module re-exports. All three were restored; focused tests passed at 118 passed and full Python suite passed at 1821 passed / 4 warnings. |
| 2026-07-09 | Clean Code Round 3 SECURE/APPROVE follow-up | RED/GREEN: `pytest tests\test_cross_engine_migration_protocol.py::test_db_core_frozen_candidate_dirs_exclude_cwd tests\test_cross_engine_migration_protocol.py::test_db_core_executable_does_not_use_path_lookup_without_dev_flag tests\test_migration_result_store.py::test_migration_result_store_auto_save_sanitizes_schema_path_components tests\test_fix_wizard_dialog.py::test_execution_page_auto_saved_rollback_stays_inside_rollback_dir -q`; focused: `pytest tests\test_cross_engine_migration_protocol.py tests\test_migration_result_store.py tests\test_fix_wizard_dialog.py tests\test_rust_core_packaging.py tests\test_path_safety.py -q`; focused review/security suite: `pytest tests\test_migration_worker.py tests\test_migration_fix_wizard.py tests\test_fix_wizard_sql_helpers.py tests\test_rust_dump_exporter.py tests\test_db_import_dialog.py tests\test_cross_engine_migration_worker.py tests\test_sql_editor_editability.py tests\test_sql_execution_worker.py -q`; `python -m py_compile` on touched Python files; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; custom product-import `MySQLConnector` allowlist scan; `git diff --check` | PASS | SECURE review found two medium issues: frozen/helper lookup could fall through to untrusted locations and schema-derived auto-save names could escape intended directories. Both are fixed with focused regression coverage. Post-fix APPROVE also restored the missing `BatchOptionDialog` legacy re-export. macOS packaging bash tests now use the discovered Git Bash path and pass at 51 passed. Full Python suite passed at 1824 passed / 4 warnings; allowlist scan found 22 product imports with no missing entries. |
| 2026-06-27 | macOS final validation tooling recheck | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_macos_final_validation_tooling_recheck -q`; `bash -n scripts/macos-manual-validation-report.sh scripts/macos-download-validation-artifacts.sh scripts/validate-macos-release.sh scripts/build-macos.sh scripts/package-macos.sh`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | #116 final validation tooling remains ready from the repository side: shell syntax is valid, macOS focused tests pass at 53, the normal gate passes, and the final gate accepts current-head manual workflow evidence while failing only for missing real-Mac report evidence under `build/`. |
| 2026-06-27 | Post-#169 next issue re-audit | `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30 --json number,title,state,url,labels,updatedAt`; `gh issue view 116 --comments --json number,title,state,body,labels,comments,updatedAt,url`; `rg -n "TODO\|FIXME\|XXX\|HACK\|NotImplemented\|raise NotImplementedError\|pass\s*$" src tests scripts docs README.md README.ko.md SCHEDULE.md`; `rg -n "not yet supported\|pending\|future\|disabled\|hidden\|preview\|manual\|not implemented\|unsupported\|준비\|미지원\|비활성\|숨김\|수동\|TODO" docs README.md README.ko.md SCHEDULE.md src tests`; `rg -n "pymysql\|psycopg\|mysql\.connector\|mysqldump\|pg_dump\|mysqlpump\|mysqlimport\|\bpsql\b\|mysqlsh\|dump tool\|external dump\|shell export\|shell import" src scripts tests docs README.md README.ko.md BUILD.md SCHEDULE.md`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_169_next_issue_reaudit -q`; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | EXPECTED FAIL for `--final` only | #116 remains the only open GitHub issue and still requires external real-Mac report evidence. The re-audit found no new repo-side issue; Rust Core-shaped connector calls route through `RustDbConnection`/`RustDbCursor`, and the lone `psql` hit is Docker live evidence seeding rather than an active export/import dump path. |
| 2026-06-27 | Superseded missing manual workflow Summary cleanup | `gh issue create` created #169; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_summary_does_not_keep_superseded_missing_manual_workflow_wording -q`; `pytest tests\test_current_status_docs.py -q`; `pytest -q`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | EXPECTED FAIL for `--final` only | GitHub #169 is fixed: Summary no longer presents older missing current-head manual workflow evidence as current state; the current #116 blocker remains missing real-Mac report evidence |
| 2026-06-27 | Focused final-gate failure reason refresh | `gh issue create` created #168; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_focused_final_gate_reason_matches_current_workflow_evidence -q`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | GitHub #168 is fixed: the current focused verification row now matches final-gate output after current-head workflow evidence refresh, so the only current final-gate failure reason is missing real-Mac manual validation report under `build/` |
| 2026-06-27 | Non-self-stale macOS workflow evidence policy | `gh issue create` created #167; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_non_self_stale_macos_workflow_evidence_policy -q` | PASS | GitHub #167 is fixed: current-status summary now treats exact current-head manual workflow run IDs/SHAs as non-durable after status-only commits and points to GitHub #116 comments plus `scripts\check-macos-support-gate.py --final` as authoritative current-head evidence |
| 2026-06-27 | Manual macOS workflow evidence refresh | `gh workflow run "macOS App Validation" --ref main`; `gh run watch 28264164795 --interval 30 --exit-status`; `gh run view 28264164795 --json status,conclusion,headSha,event,workflowName,url,createdAt,updatedAt,jobs`; `python scripts\check-macos-support-gate.py --final`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_manual_macos_workflow_evidence -q` | EXPECTED FAIL for `--final` only | Manual `macOS App Validation` workflow_dispatch run `28264164795` passed for then-current main HEAD `6ad09590bf14d678a568fd64ac74765fd1eff0c9`, including arm64 and x86_64. Final gate accepted that workflow evidence for that HEAD and failed only because no real-Mac manual validation report was present under `build/`; rerun after status-only commits if main advances. |
| 2026-06-27 | Post-#166 next issue re-audit | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_166_next_issue_reaudit -q`; `git status --short --branch`; `gh issue list --state open --limit 30`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; Rust Core baseline and stale handoff scans | EXPECTED FAIL for `--final` only | `main` was aligned with `origin/main`; #116 is the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for current merged main HEAD, so no new repo-side implementation issue was created. |
| 2026-06-27 | Scheduled backup tuple connection info | RED/GREEN: `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_accepts_tuple_connection_info_for_rust_dump -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_tuple_connection_issue -q`; `gh issue create` created #166 | PASS | GitHub #166 is fixed: scheduled Rust dump backups now accept real `TunnelEngine.get_connection_info()` tuple output and resolve DB credentials outside the connection-info tuple before creating `RustDumpConfig` |
| 2026-06-27 | Scheduled backup PostgreSQL engine | `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30`; `rg -n "_execute_backup\|RustDumpConfig\|db_engine\|get_connection_info\|tunnel_configs\|export_full_schema\|export_tables" src\core\scheduler.py tests\test_scheduler.py`; `gh issue create` created #165; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_scheduled_backup_postgresql_engine_issue -q`; RED/GREEN: `pytest tests\test_scheduler.py::TestBackupScheduler::test_backup_task_preserves_postgresql_engine_for_rust_dump -q` | PASS | GitHub #165 is fixed: scheduled Rust dump backups now normalize tunnel `db_engine` metadata and pass it into `RustDumpConfig`, preserving PostgreSQL while keeping the MySQL default fallback |
| 2026-06-27 | PostgreSQL dump wrapper engine | RED/GREEN: `pytest tests\test_rust_dump_exporter.py -q -k "wrapper_preserves_postgresql_engine"`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_dump_wrapper_engine_issue -q`; `gh issue create` created #164; `pytest -q` | PASS | GitHub #164 is fixed: `export_schema`, `export_tables`, and `import_dump` convenience wrappers preserve PostgreSQL engine into `RustDumpConfig`; full-suite count is superseded by TF-STATUS-067 |
| 2026-06-27 | PostgreSQL Import timezone Core validation | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_mysql_and_postgresql_timezone_forms --lib`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_core_validation_issue -q`; `gh issue create` created #163; `pytest -q`; `cargo test --manifest-path migration_core\Cargo.toml` | PASS | GitHub #163 is fixed: Rust Core `dump.import` accepts PostgreSQL `SET TIME ZONE` timezone SQL as well as MySQL `SET SESSION time_zone`, while preserving single-statement and safe-literal validation; full-suite count is superseded by TF-STATUS-066 |
| 2026-06-27 | PostgreSQL Import timezone SQL | RED/GREEN: `pytest tests\test_db_dialogs.py -q -k "postgresql_import_auto_timezone or postgresql_import_forced_kst"`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_import_timezone_issue -q`; i18n regression: `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_db_dialogs.py -q -k "direct_hardcoded or postgresql_import_auto_timezone or postgresql_import_forced_kst"`; `gh issue create` created #162; `pytest -q` | PASS | GitHub #162 is fixed: PostgreSQL dump import default auto timezone mode skips MySQL timezone table detection and sends no MySQL timezone correction SQL; forced KST/UTC use PostgreSQL `SET TIME ZONE`; full-suite count is superseded by TF-STATUS-065 |
| 2026-06-27 | PostgreSQL Rust dump endpoint engine | RED/GREEN: `pytest tests\test_rust_dump_exporter.py::TestRustDumpConfig::test_config_preserves_postgresql_engine tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_full_schema_preserves_postgresql_engine_in_rust_payload tests\test_rust_dump_exporter.py::TestRustDumpImporter::test_import_dump_preserves_postgresql_engine_in_rust_payload -q`; RED/GREEN: `pytest tests\test_db_dialogs.py::test_preselected_export_tunnel_uses_postgres_connector_for_postgresql tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_postgresql_rust_dump_engine_issue -q`; `gh issue create` created #161; `pytest -q` | PASS | GitHub #161 is fixed: `RustDumpConfig` preserves `engine`, PostgreSQL Export/Import dialogs pass `PostgresConnector.engine`, preselected PostgreSQL tunnels construct `PostgresConnector`, and Rust Core `dump.run`/`dump.import` payloads use `postgresql` endpoints; full-suite count is superseded by TF-STATUS-064 |
| 2026-06-27 | Partial export FK parent resolution | RED/GREEN: `pytest tests\test_rust_dump_exporter.py::TestRustDumpExporter::test_export_tables_resolves_fk_parents_through_rust_schema_inspect -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_partial_export_fk_parent_rust_inspect_issue -q`; `gh issue create` created #160; `pytest tests\test_rust_dump_exporter.py -q`; `pytest -q` | PASS | GitHub #160 is fixed: `RustDumpExporter.export_tables(... include_fk_parents=True)` now uses Rust Core-owned `schema.inspect` to include transitive FK parent tables before `dump.run`, without instantiating Python `MySQLConnector`; full-suite count is superseded by TF-STATUS-063 |
| 2026-06-27 | Current-status baseline provenance refresh | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_baseline_provenance_uses_latest_status_update -q`; `gh issue create` created #159; `pytest tests\test_current_status_docs.py -q`; `pytest -q` | PASS | GitHub #159 is fixed: top current-status baseline provenance now refers to the latest status update instead of stale post-#156 wording; full-suite count is superseded by TF-STATUS-062 |
| 2026-06-27 | SQL dollar quote helper None input guard | RED/GREEN: `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_none_sql_text -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_dollar_quote_none_input_issue -q`; `gh issue create` created #158; `pytest tests\test_sql_execution_worker.py tests\test_sql_editor_dialog.py tests\test_scheduler.py -q`; `pytest -q` | PASS | GitHub #158 is fixed: `read_dollar_quote(None, 0)` and `SQLExecutionWorker._read_dollar_quote(None, 0)` now fail closed with `""` instead of raising `TypeError`; full-suite count is superseded by TF-STATUS-061 |
| 2026-06-27 | One-Click readiness next-action wording cleanup | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_does_not_present_closed_issues_as_current_tracking -q`; RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_oneclick_next_action_wording_issue -q`; `gh issue create` created #157; `pytest -q` | PASS | GitHub #157 is fixed: `docs/oneclick_readiness.md` now frames additional One-Click automatic-fix work as standing policy/watch guidance instead of a current `Recommended next repo-side change`; full-suite count is superseded by TF-STATUS-060 |
| 2026-06-27 | Post-#156 main merge and next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_156_next_issue_analysis -q`; `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30 --json number,title,labels,url,updatedAt`; `gh issue view 116 --json number,title,state,labels,body,comments,url,updatedAt`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `pytest -q` | EXPECTED FAIL for `--final` only | `main` was already aligned with `origin/main`; #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for current merged main HEAD, so no new repo-side implementation issue was created. Full-suite count is superseded by TF-STATUS-060 |
| 2026-06-27 | SQL dollar quote helper guard | RED/GREEN: `pytest tests\test_sql_execution_worker.py::test_dollar_quote_reader_fails_closed_for_out_of_range_starts -q`; `gh issue create` created #156; `pytest tests\test_sql_execution_worker.py tests\test_sql_editor_dialog.py tests\test_scheduler.py -q`; `python -m compileall -q src\core\sql_statement_parser.py tests\test_sql_execution_worker.py`; `git diff --check` | PASS | GitHub #156 is fixed: `read_dollar_quote` and `SQLExecutionWorker._read_dollar_quote` return `""` for empty SQL text or out-of-range start offsets |
| 2026-06-27 | SQL statement parser mismatch fix | RED/GREEN: `pytest tests\test_sql_editor_dialog.py::test_split_queries_preserves_comments_dollar_quotes_and_delimiters tests\test_sql_editor_dialog.py::test_get_query_at_cursor_uses_statement_parser_ranges -q`; RED/GREEN: `pytest tests\test_scheduler.py::TestBackupScheduler::test_parse_sql_queries_preserves_comments_dollar_quotes_and_delimiters -q`; `pytest tests\test_sql_editor_dialog.py tests\test_scheduler.py tests\test_sql_execution_worker.py -q`; `pytest -q` | PASS | GitHub #155 is fixed: SQL file execution, SQL Editor execute-all/current-query, and scheduled SQL now share `src/core/sql_statement_parser.py`; SQL Editor current-query lookup uses parser ranges via `find_sql_statement_at_position` |
| 2026-06-27 | SQL statement parser mismatch analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_sql_statement_parser_mismatch_issue -q`; `git fetch --all --prune`; `git status --short --branch`; `gh issue list --state open --limit 30`; `gh issue view 116 --json ...`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; direct parser comparison; `gh issue create` created #155 | EXPECTED FAIL for `--final` only | `main` is aligned with `origin/main`; #116 remains external real-Mac evidence work; GitHub #155 now tracks the confirmed repo-side mismatch where SQL Editor/Scheduler quote-only splitting diverges from the robust SQL file execution parser |
| 2026-06-27 | Call-local Rust cursor affected-row metadata | RED/GREEN: `pytest tests\test_db_core_service.py::test_rust_db_cursor_rowcount_uses_call_local_rows_affected -q`; `gh issue create` created #154; focused DB core/current-status pytest; `pytest -q` | PASS | GitHub #154 is fixed: `RustDbCursor.rowcount` uses call-local `execute_on_connection_result` metadata instead of shared facade state; the then-current 1839-test suite evidence is superseded by TF-STATUS-056 |
| 2026-06-27 | Rust Core DML affected row counts | RED/GREEN: `pytest tests\test_db_core_service.py::test_rust_db_cursor_rowcount_uses_core_rows_affected_for_dml -q`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml query_result_includes_non_row_rows_affected --lib`; `gh issue create` created #153; focused Python/Rust query result tests; `cargo test --manifest-path migration_core\Cargo.toml`; `pytest -q` | PASS | GitHub #153 is fixed: Rust Core query execution carries `rows_affected` metadata and `RustDbCursor.rowcount` preserves it for scheduled SQL and SQL editor DML reporting; the then-current 1839-test suite evidence is superseded by TF-STATUS-056 |
| 2026-06-27 | Post-#151 full-suite evidence refresh | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_151_full_pytest_refresh_issue tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count tests\test_current_status_docs.py::test_current_status_does_not_describe_stale_full_pytest_count_as_current -q`; `gh issue create` created #152; `pytest -q` | PASS | GitHub #152 is fixed: the suite evidence was refreshed to 1839 tests, stale 1832/1834/1835/1837-count wording cannot return as current evidence, and the count is now superseded by TF-STATUS-056 |
| 2026-06-27 | Post-#151 main merge and next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_151_next_issue_analysis -q`; `git fetch --all --prune`; `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 20`; `gh issue view 116 --json number,title,state,labels,milestone,updatedAt,url,body`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | `main` was aligned with `origin/main` before this status update, and this status update was pushed to `origin/main`; #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for current merged main HEAD, so no new repo-side implementation issue was created |
| 2026-06-27 | Stale current pytest count wording cleanup | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_describe_stale_full_pytest_count_as_current -q`; `gh issue create` created #151; stale full-suite wording scan in `docs\current_status.md` and `tests\test_current_status_docs.py` | PASS | GitHub #151 is fixed: older TF-STATUS-049 wording no longer describes the prior full-suite count as current evidence; current full-suite evidence is superseded above |
| 2026-06-27 | RustDbCursor executemany batch helper fail-closed | RED/GREEN: `pytest tests\test_db_core_service.py::test_rust_db_cursor_executemany_rejects_python_batch_helper -q`; `gh issue create` created #150; `pytest tests\test_current_status_docs.py::test_current_status_tracks_rust_db_cursor_executemany_issue -q`; `rg -n "executemany\(|execute_many\(" src tests migration_core\src migration_core\tests`; `pytest -q` | PASS | GitHub #150 is fixed: `RustDbCursor.executemany` now rejects the unused Python-side batch helper before any query/facade call; single-query Rust Core paths remain unchanged; full-suite evidence is superseded above |
| 2026-06-27 | Post-v2.1.7 version drift fix | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_v217_version_drift_issue -q`; `gh issue create` created #149; `git rev-list --count v2.1.7..HEAD`; `gh release list --limit 5`; `python scripts\bump_version.py --bump-type patch`; `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q`; `pytest -q` | PASS | GitHub #149 is fixed: current source/package/installer version is `2.1.8`, ahead of already published release `v2.1.7` after main accumulated release-tracking commits; its full-suite count is superseded by the current evidence above |
| 2026-06-27 | Post-#148 next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_148_next_issue_analysis -q`; `git status --short --branch`; `gh issue list --state open --limit 20`; `gh issue view 116 --json number,title,state,labels,body,comments,url,updatedAt`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | Current `main` is aligned with `origin/main`; #116 is the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for the current merged main HEAD, so no new repo-side implementation issue was created |
| 2026-06-27 | v2.1.7 release publication | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_v217_release_publication_issue -q`; `git status --short --branch`; `gh issue list --state open --limit 20`; `git tag -a v2.1.7 -m "Release v2.1.7"`; `git push origin v2.1.7`; `gh run view 28255274238 --json status,conclusion,url`; `gh release view v2.1.7 --json tagName,name,url,assets,publishedAt,targetCommitish,isDraft,isPrerelease` | PASS | GitHub #148 is fixed: release `v2.1.7` was published from current `main` with `TunnelForge-Setup-2.1.7.exe`, `TunnelForge-WebSetup.exe`, `TunnelForge-macOS-2.1.7-arm64.dmg`, `TunnelForge-macOS-2.1.7-arm64.zip`, `TunnelForge-macOS-2.1.7-x86_64.dmg`, `TunnelForge-macOS-2.1.7-x86_64.zip`, and checksum assets |
| 2026-06-27 | Post-release version drift fix | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_post_release_version_drift_issue -q`; `gh issue create` created #147; `python scripts\bump_version.py --bump-type patch`; `pytest tests\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q`; `git log --oneline v2.1.6..HEAD`; `gh release list --limit 10` | PASS | GitHub #147 is fixed: current source/package/installer version is `2.1.7` after `v2.1.6` was already released and main accumulated post-release commits |
| 2026-06-27 | Post-#146 next issue analysis | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_146_next_issue_analysis -q`; `git status --short --branch`; `gh issue list --state open --limit 30`; `gh issue view 116 --json number,title,state,labels,body,comments,url`; direct DB mutation/helper scan; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final` | EXPECTED FAIL for `--final` only | Current `main` is aligned with `origin/main`; #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate fails only for missing real-Mac report under `build/` and missing successful manual `macOS App Validation` workflow_dispatch evidence for the current merged main HEAD, so no new repo-side implementation issue was created |
| 2026-06-27 | Legacy MySQLConnector execute_many mutation helper fail-closed | RED/GREEN: `pytest tests\test_db_connector.py::TestMySQLConnector::test_execute_many_rejects_legacy_python_mutation_helper -q`; `gh issue create` created #146; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_execute_many_issue -q`; final: `pytest tests\test_db_connector.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\db_connector.py tests\test_db_connector.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #146 is fixed: `MySQLConnector.execute_many` now rejects the unused Python batch mutation helper before cursor/commit work, while read/query helper behavior is unchanged |
| 2026-06-27 | Legacy CleanupWorker actual cleanup mode fail-closed | RED/GREEN: `pytest tests\test_migration_worker.py -q`; `gh issue create` created #145; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_cleanup_worker_issue -q`; final: `pytest tests\test_migration_worker.py tests\test_migration_analyzer.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\workers\migration_worker.py tests\test_migration_worker.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #145 is fixed: `CleanupWorker(..., dry_run=False)` rejects legacy actual cleanup mode before a thread can start, while dry-run cleanup worker construction remains available |
| 2026-06-27 | Legacy MigrationAnalyzer cleanup mutations fail-closed | RED/GREEN: `pytest tests\test_migration_analyzer.py::TestExecuteCleanup::test_actual_cleanup_rejects_legacy_python_mutation_mode tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q`; `pytest tests\test_migration_analyzer.py::TestExecuteCleanup tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled -q`; `gh issue create` created #144; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_migration_analyzer_cleanup_issue -q`; i18n regression: `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation ... -q`; final: `pytest tests\test_migration_analyzer.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\i18n.py src\core\migration_analyzer.py src\ui\dialogs\migration_dialogs.py tests\test_migration_analyzer.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #144 is fixed: `MigrationAnalyzer.execute_cleanup(..., dry_run=False)` rejects Python-owned cleanup mutation mode, the migration analyzer dialog keeps actual cleanup execution disabled until Rust Core owns it, and Dry-Run and SQL preview remain available |
| 2026-06-27 | Legacy Auto-Fix core mutation APIs fail-closed | RED/GREEN: `pytest tests\test_migration_fix_wizard.py::TestSessionGuardFaultInjection::test_batch_executor_rejects_legacy_python_mutation_mode tests\test_migration_fix_wizard.py::TestSessionGuardFaultInjection::test_fk_safe_charset_changer_rejects_legacy_python_mutation_mode -q`; RED/GREEN: `pytest tests\test_migration_fix_wizard.py::TestSessionGuardFaultInjection::test_private_single_execution_hook_is_fail_closed -q`; `pytest tests\test_migration_fix_wizard.py -q`; `gh issue create` created #143; `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_auto_fix_core_mutation_api_issue -q`; final: `pytest tests\test_migration_fix_wizard.py tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\migration_fix_wizard.py tests\test_migration_fix_wizard.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #143 is fixed: `BatchFixExecutor.execute_batch`, `FKSafeCharsetChanger.execute_safe_charset_change`, and `BatchFixExecutor._execute_single` now reject Python-owned DB mutation/session execution; dry-run/SQL preview remains available; current full Python suite is superseded above by the 1827-test run |
| 2026-06-27 | Post-#142 next issue analysis | `gh issue list --state open --limit 30 --json number,title,state,labels,updatedAt,url,assignees`; `gh issue view 116 --comments --json number,title,state,body,labels,comments,updatedAt,url`; `python scripts\check-macos-support-gate.py`; `python scripts\check-macos-support-gate.py --final`; `rg -n "#116|TF-STATUS-008|real-Mac|real Mac|Mac validation|macOS Support M6|manual validation|final" docs\current_status.md docs\macos_support.md scripts tests README.md README.ko.md` | EXPECTED FAIL for `--final` only | #116 is still the only open GitHub issue. Normal repo-side gate passes. Final gate currently reports `no macOS manual validation report found under build/` and `no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD`, so the blocker remains external real-Mac evidence/current-head manual validation, not a repo-side implementation issue |
| 2026-06-27 | Legacy Auto-Fix Wizard dry-run only | RED/GREEN: `pytest tests\test_fix_wizard_dialog.py::test_legacy_fix_wizard_execution_page_runs_dry_run_only -q`; RED/GREEN: `pytest tests\test_fix_wizard_dialog.py::test_fix_wizard_worker_rejects_legacy_python_mutation_mode -q`; `pytest tests\test_fix_wizard_dialog.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q`; `pytest tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\core\i18n.py src\ui\dialogs\fix_wizard_dialog.py src\ui\workers\fix_wizard_worker.py tests\test_fix_wizard_dialog.py tests\test_current_status_docs.py`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py`; `pytest -q`; `git diff --check` | PASS | GitHub #142 is fixed: `ExecutionPage` now starts `FixWizardWorker` with `dry_run=True`, `FixWizardWorker` rejects `dry_run=False`, and the legacy Auto-Fix UI presents Dry-run/SQL/manual execution rather than direct DB mutation; English runtime translations cover the new UI copy; current full Python suite is superseded above by the 1827-test run |
| 2026-06-27 | Legacy Python Auto-Fix Wizard mutation issue split | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_tracks_legacy_python_auto_fix_wizard_issue -q`; `gh issue create` created #142; `pytest -q`; `rg -n "MigrationFixWizard|FixWizard|fix_wizard|btn_auto_fix|auto_fix|MigrationAnalyzerDialog|migration_dialogs|oneclick|One-Click" src tests docs README.md README.ko.md`; inspection of `src/ui/dialogs/migration_dialogs.py`, `src/ui/dialogs/fix_wizard_dialog.py`, `src/ui/workers/fix_wizard_worker.py`, and `src/core/migration_fix_wizard.py` | PASS | GitHub #142 tracked the repo-side Rust Core baseline gap where the legacy Auto-Fix Wizard could execute DB mutations through Python-owned fix logic; this count is superseded above by the 1827-test run |
| 2026-06-27 | Post-merge next-issue external re-audit | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_post_merge_next_issue_external_reaudit -q`; `git status --short --branch`; `gh issue list --state open --limit 30 --json number,title,state,labels,updatedAt,url,assignees`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py`; `pytest -q`; direct DB/feature-flag/stale-doc scans | PASS | At that pass #116 was the only open issue, the #116 repo-side gates passed, SQL editor query execution also routed through the Rust connector shim, and no new GitHub issue was created because no confirmed repo-side issue was found yet |
| 2026-06-27 | macOS manual workflow head policy | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_manual_workflow_after_pr_merge -q`; RED/GREEN: `pytest tests\test_macos_support_docs.py -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python scripts\check-macos-support-gate.py`; `gh issue view 116 --json body` | PASS | `scripts/check-macos-support-gate.py --final` now resolves the successful manual `workflow_dispatch` macOS artifact run against the same head policy as report SHA/artifact download: PR head before merge, current merged main HEAD after PR #117 has merged; GitHub #116 body now says the same, and current macOS focused suite is 53 passed |
| 2026-06-27 | BUILD installer version examples | RED/GREEN: `pytest tests\test_build_docs.py tests\test_current_status_docs.py::test_current_status_records_build_doc_installer_version_cleanup -q`; final: `pytest -q`; `pytest tests\test_build_docs.py tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_build_docs.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | `BUILD.md` no longer shows stale 1.0.0 installer filename/AppVersion examples; installer examples use `{version}` and `AppVersion={#MyAppVersion}`; the current full Python suite count is superseded above by the 1827-test run |
| 2026-06-27 | One-Click module scope docstring | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_module_docstring_matches_limited_rust_core_scope tests\test_current_status_docs.py::test_current_status_records_oneclick_module_scope_docstring_cleanup -q`; final: `pytest -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | Module-level One-Click wording no longer says the whole migration process is automatically executed; it now describes Rust DB Core dry-run default and limited real execution; the current full Python suite count is superseded above by the 1827-test run |
| 2026-06-27 | One-Click fallback dry-run tooltip | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_disabled_real_execution_tooltip_does_not_reference_closed_138 tests\test_current_status_docs.py::test_current_status_records_oneclick_fallback_dry_run_tooltip_cleanup -q`; final: `pytest -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | The disabled-real-execution fallback tooltip now says real execution is `disabled in this build` and no longer points at the already closed GitHub #138 gate; the current full Python suite count is superseded above by the 1827-test run |
| 2026-06-27 | Rust Core Export/Import menu wording | RED/GREEN: `pytest tests\test_main_window_export_import_labels.py -q`; `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count tests\test_current_status_docs.py::test_current_status_records_rust_core_export_import_menu_wording -q`; final: `pytest -q`; `pytest tests\test_main_window_export_import_labels.py tests\test_current_status_docs.py -q`; `python -m compileall -q src\ui\main_window.py tests\test_main_window_export_import_labels.py tests\test_current_status_docs.py`; `git diff --check`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py` | PASS | Tunnel context menu actions now display `Rust DB Core Export` / `Rust DB Core Import`, handlers use `_context_rust_core_export` / `_context_rust_core_import`; the current full Python suite count is superseded above by the 1827-test run |
| 2026-06-27 | Current baseline duplicate service.hello cleanup | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_current_baseline_has_no_duplicate_check_rows -q`; `pytest tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | `Current Baseline Verification` now keeps one `tunnelforge-core service.hello` row that covers dump/import, migration, and One-Click capability evidence |
| 2026-06-27 | Focused verification duplicate row cleanup | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_focused_verification_has_no_duplicate_check_rows -q`; `pytest tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | `Focused Verification On 2026-06-27` no longer repeats the same `python scripts\check-macos-support-gate.py --skip-github` check row |
| 2026-06-27 | Current baseline count refresh after re-audit coverage | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_macos_focused_test_count tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q`; `pytest -q`; `pytest tests\test_current_status_docs.py -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | Top current baseline reflects the refreshed current-status coverage and macOS focused suite evidence; the current `pytest -q` row is superseded above by the 1827-test run, and macOS focused tests are now superseded by the 53-test run |
| 2026-06-27 | Current main next-issue re-audit | `git status --short --branch`; `git log --oneline --decorate -5`; `gh issue list --state open --limit 20`; `gh issue view 116 --comments`; `rg -n "pymysql|psycopg|mysql\.connector|mysqldump|pg_dump|mysqlpump|mysqlimport|\bpsql\b" src scripts`; `rg -n "execute\(|cursor\(|commit\(|rollback\(" src\core src\ui src\exporters`; `python scripts\check-macos-support-gate.py --skip-github`; `python scripts\check-macos-support-gate.py`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS | Main is aligned with origin/main, #116 is the only open GitHub issue, #116 repo-side gates pass, macOS focused tests now pass at 53 tests, and the Rust Core baseline scan found no new repo-side violation; legacy-shaped DB connector paths route through Rust Core shims |
| 2026-06-27 | Current baseline verification heading | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_current_baseline_section_is_not_stale_dated -q`; `pytest tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py`; `git diff --check` | PASS | Top status no longer labels the mixed current baseline as `Verified On 2026-06-26`; the section now distinguishes the refreshed 2026-06-27 full-suite count from preserved 2026-06-26 broader baseline evidence |
| 2026-06-27 | Current full Python suite count refresh | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q`; `pytest -q`; `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py tests\test_schedule_docs.py -q`; `python -m compileall -q tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py tests\test_schedule_docs.py`; `git diff --check` | PASS | Updated top current-status full Python suite count from stale `1826 passed` to current `1827 passed, 5 warnings` after the post-release version drift regression test was added |
| 2026-06-27 | One-Click limited production scope wording | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_distinguishes_limited_real_execution_from_broad_production_support -q`; `pytest tests\test_oneclick_readiness_docs.py -q`; `pytest tests\test_oneclick_readiness_docs.py tests\test_current_status_docs.py -q`; `python -m compileall -q tests\test_oneclick_readiness_docs.py tests\test_current_status_docs.py`; `git diff --check` | PASS | Readiness docs no longer say all production database usage is unsupported; they distinguish the current backup-confirmed `engine_innodb` real-execution path from unsupported broad production automatic remediation and production charset/collation execution |
| 2026-06-27 | Schedule guide hidden-feature wording | RED/GREEN: `pytest tests\test_schedule_docs.py -q`; `pytest tests\test_schedule_docs.py tests\test_current_status_docs.py -q`; `rg -n -F -e '메인 툴바에서 **"스케줄"** 버튼을 클릭' -e '스케줄 시간을 기다리지 않고 바로 백업하려면:' -e '스케줄 관리 창의 **"백업 로그"** 탭에서' -e '스케줄이 작동하려면 TunnelForge가 실행 중이어야 합니다' SCHEDULE.md`; `python -m compileall -q tests\test_schedule_docs.py tests\test_current_status_docs.py`; `git diff --check` | PASS | `SCHEDULE.md` now reads as an internal/reactivation memo while `SCHEDULE_FEATURE_ENABLED = False`, and no longer gives public-toolbar/log/immediate-run instructions as current user steps |
| 2026-06-27 | macOS artifact default source after PR #117 merge | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_uses_local_head_after_pr_merge -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `bash -n scripts/macos-download-validation-artifacts.sh scripts/macos-manual-validation-report.sh`; `pytest tests\test_current_status_docs.py -q`; `python scripts\check-macos-support-gate.py --skip-github`; `python -m compileall -q tests\test_rust_core_packaging.py tests\test_macos_support_docs.py tests\test_current_status_docs.py scripts\check-macos-support-gate.py`; `git diff --check` | PASS | `macos-download-validation-artifacts.sh` now finds the latest successful manual `macOS App Validation` run for PR head before merge, or current merged main HEAD after PR #117 is merged, so downloaded artifact provenance matches the final report/gate SHA policy |
| 2026-06-26 | Direct DB Export/Import Rust Core endpoint host | RED/GREEN: `pytest tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump -q`; RED/GREEN: `pytest tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q`; `pytest tests\test_db_dialogs.py::test_export_dialog_uses_direct_connector_host_for_rust_dump tests\test_db_dialogs.py::test_import_dialog_uses_direct_connector_host_for_rust_dump -q` | PASS | `RustDumpExportDialog` and `RustDumpImportDialog` now preserve direct connector `host` when creating `RustDumpConfig`; tunnel connections still use their connector host, normally `127.0.0.1` |
| 2026-06-26 | Export table selection audit | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_export_table_selection_audit -q`; `git status --short --branch`; `gh issue list --state open --limit 30 --json number,title,state,labels,updatedAt,url`; `rg -n "class RustDumpExportDialog|export_tables|dump.run|selected_tables|table selection" src tests docs README.md README.ko.md migration_core\src migration_core\tests`; code inspection of `RustDumpExportDialog`, `RustDumpExporter.export_tables`, `RustDumpWorker`, and Rust `dump.run` table filtering | PASS | Export individual table selection is currently implemented: PyQt exposes `선택 테이블 Export`, checkbox table list, select-all/deselect-all, and FK parent auto-include; Python forwards selected tables through `RustDumpExporter.export_tables`; Rust Core `dump.run` filters schema tables from the `tables` payload |
| 2026-06-26 | GitHub #116 final evidence attachment wording | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_can_post_github_comment -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py tests\test_current_status_docs.py -q`; `python scripts\check-macos-support-gate.py`; `python -m compileall -q tests\test_rust_core_packaging.py tests\test_macos_support_docs.py tests\test_current_status_docs.py scripts\check-macos-support-gate.py`; `git diff --check` | PASS | Finalizer stdout, generated GitHub comment, and macOS support docs now tell operators to attach final real-Mac evidence to #116 first; PR #117 is only a traceability mirror |
| 2026-06-26 | GitHub #116 Actions lookup command accuracy | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_rejects_hard_coded_current_issue_head -q`; RED/GREEN: `python scripts\check-macos-support-gate.py`; `gh run list --workflow "macOS App Validation" --branch main --limit 5`; `gh run list --workflow "macOS App Validation" --event pull_request --limit 3`; `gh run list --workflow "Version Gate" --event pull_request --limit 3`; `gh issue edit 116 --body-file <temp>` | PASS | #116 no longer tells operators to use `--branch main` for PR workflow run lookup; event-filtered commands return relevant PR/manual runs |
| 2026-06-26 | Current full Python suite count refresh | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `pytest -q` | PASS | Updated top current-status full Python suite count from stale `1729 passed` to current `1786 passed, 5 warnings` |
| 2026-06-26 | Current macOS focused test count refresh | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `pytest tests\test_current_status_docs.py tests\test_macos_support_docs.py tests\test_oneclick_readiness_docs.py -q` | PASS | Updated top current-status macOS focused test count from stale `47 passed` to current `51 passed` |
| 2026-06-26 | GitHub #116 non-volatile Actions run wording | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_rejects_hard_coded_current_issue_head -q`; RED/GREEN: `python scripts\check-macos-support-gate.py`; `gh issue view 116 --json body`; `gh issue edit 116 --body-file <temp>` | PASS | Gate now rejects #116 Current Evidence lines that label fixed GitHub Actions run URLs as `Latest`; issue body now uses reference-run wording and lets the gate resolve current matching runs |
| 2026-06-26 | GitHub #116 non-volatile current head policy | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_rejects_hard_coded_current_issue_head -q`; RED/GREEN: `python scripts\check-macos-support-gate.py`; `gh issue view 116 --json body`; `gh issue edit 116 --body-file <temp>` | PASS | Gate now rejects #116 body wording that hard-codes a current gate head SHA; #116 Current Evidence now tells operators to use latest pushed `main` / `origin/main` instead |
| 2026-06-26 | GitHub #116 current head refresh after docs commits | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `gh issue view 116 --json body,updatedAt,url`; `gh issue edit 116 --body-file <temp>`; `gh issue view 116 --json body --jq .body` | PASS | #116 Current Evidence is refreshed to the latest pushed `main` / gate head and clarifies final reports match PR head before merge or current merged main after merge |
| 2026-06-26 | One-Click evidence README completion wording | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; `rg -n "future|template|completed|oneclick-real-execution-evidence|oneclick-charset-evidence|oneclick-charset-derivation-evidence|#138|#139|#140" reports\oneclick_readiness docs\oneclick_readiness.md tests` | PASS | Evidence README no longer describes completed #138/#139 local evidence as future work; templates are documented as refresh shapes, not missing evidence |
| 2026-06-26 | One-Click closed-issue wording drift | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; `rg -n "TODO|FIXME|XXX|HACK|NotImplemented|raise NotImplementedError|pass\s*$" src tests scripts docs README.md README.ko.md BUILD.md SCHEDULE.md`; `rg -n "not yet supported|pending|future|disabled|hidden|preview|manual|not implemented|unsupported|준비|미지원|비활성|숨김|수동|remaining|still needs|still requires|open issue|blocked" docs README.md README.ko.md BUILD.md SCHEDULE.md src tests scripts`; `rg -n "#1(1[0-9]|2[0-9]|3[0-9]|4[0-9])|TF-STATUS-[0-9]+|Next action:" docs README.md README.ko.md BUILD.md SCHEDULE.md reports scripts tests src` | PASS | Fresh repo-side scan found stale current-tense One-Click tracking wording for closed #138/#139; readiness doc now states #137-#141 are completed and no One-Click follow-up issue is open |
| 2026-06-26 | macOS artifact head SHA provenance | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_writes_env_file -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_check_complete_rejects_missing_artifact_metadata -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_checks_report_artifact_head_sha -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate -q`; `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_script_accepts_local_final_report tests\test_rust_core_packaging.py::test_macos_support_gate_script_checks_report_artifact_workflow_run tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate -q` | PASS | Final macOS evidence and generated GitHub evidence comment now record and gate-check the artifact workflow head SHA separately from the report Git SHA |
| 2026-06-26 | GitHub #116 handoff body refresh | `gh issue view 116 --json body`; `gh issue edit 116 --body-file <temp>`; `gh issue view 116 --json body --jq .body` | PASS | Historical refresh: #116 Current Evidence then pointed operators at gate head `6da13f7` and no longer said PR #117 still needed to be marked ready |
| 2026-06-26 | macOS final report SHA after PR #117 merge | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_final_report_after_pr_merge -q`; RED/GREEN: `pytest tests\test_macos_support_docs.py -q`; `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state tests\test_rust_core_packaging.py::test_macos_support_gate_script_accepts_local_final_report tests\test_rust_core_packaging.py::test_macos_support_gate_script_rejects_report_from_different_git_sha -q`; `python scripts\check-macos-support-gate.py` | PASS | Final gate now expects the current merged main HEAD for report Git SHA after PR #117 is merged, instead of the stale PR head |
| 2026-06-26 | macOS support gate after PR #117 merge | `python scripts\check-macos-support-gate.py` failed before fix because merged PR #117 reports `mergeStateStatus=UNKNOWN`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state -q`; `python scripts\check-macos-support-gate.py`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q scripts\check-macos-support-gate.py tests\test_rust_core_packaging.py`; `git diff --check` | PASS | Full GitHub #116 gate now accepts merged PR #117 while still checking issue state and status checks |
| 2026-06-26 | Current status stale handoff scan | `rg -n "TODO|FIXME|XXX|HACK|NotImplemented|raise NotImplementedError|pass\s*$" src tests scripts docs README.md README.ko.md SCHEDULE.md`; `rg -n "not yet supported|pending|future|disabled|hidden|preview|manual|not implemented|unsupported|준비|미지원|비활성|숨김|수동" docs README.md README.ko.md SCHEDULE.md src tests`; `rg -n "GitHub issue #[0-9]+ now tracks|next actionable|remaining unchecked|should remain open|still requires|still needs|TODO" docs README.md README.ko.md reports scripts tests`; `pytest tests\test_current_status_docs.py -q` RED/GREEN | PASS | Found no new repo-side issue beyond #116; corrected stale top-handoff wording that still presented closed #140 as current work |
| 2026-06-26 | Current status summary consistency and next issue analysis | `pytest tests\test_current_status_docs.py -q` RED/GREEN; `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py -q`; `gh issue list --state open --limit 30 --json number,title,labels,updatedAt,url`; `gh issue view 116 --json number,title,state,body,labels,comments,url,updatedAt`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `git diff --check` | PASS | Summary now matches GitHub state: #116 is the only open issue and #137-#141 are closed One-Click readiness work; no additional repo-side #116 gap found |
| 2026-06-26 | One-Click PyQt charset derivation evidence | `pytest tests\test_oneclick_charset_derivation_evidence.py -q` RED/GREEN; `pytest tests\test_oneclick_charset_derivation_capture.py -q` RED/GREEN; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py tests\test_oneclick_charset_derivation_capture.py tests\test_oneclick_charset_derivation_evidence.py -q`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-charset-derivation-evidence.py --seed-local-container --mysql-container tf-live-mysql --mysql-host 127.0.0.1 --mysql-port 3406 --mysql-user root --mysql-password test --schema tf_oneclick_derive_charset --output reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; `python scripts\validate-oneclick-charset-derivation-evidence.py reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_DERIVATION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | #140 local evidence proves PyQt-triggered Rust Core derivation feeds `oneclick.run dry_run=false` and converts 2 FK-connected local tables |
| 2026-06-26 | One-Click follow-up issue split | `rg -n "invalid_date|zerofill_usage|float_precision|int_display_width|enum_empty_value|manual|skip|oneclick_issues_from_inspection|oneclick_recommendations|oneclick_auto_fix_option" migration_core\src\lib.rs docs\oneclick_readiness.md tests docs\current_status.md`; `gh issue create` created #141 | PASS | `int_display_width` skip semantics are now tracked separately from closed #140 |
| 2026-06-26 | One-Click `int_display_width` skip policy | `pytest tests\test_oneclick_readiness_docs.py -q` RED/GREEN; `pytest tests\test_oneclick_readiness_docs.py tests\test_oneclick_rust_core_gate.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick_live_inspection_does_not_synthesize_int_display_width_skip --lib`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` | PASS | #141 policy is now explicit: externally supplied `skip` is display-only and Rust Core live One-Click does not synthesize or execute this class |
| 2026-06-26 | Main merge/status and then-current next issue analysis | `git fetch origin --prune`; `git status --short --branch`; `gh issue list --state open --limit 20 --json number,title,labels,updatedAt,assignees,url`; `gh issue view 140 --comments --json number,title,state,body,comments,labels,url,updatedAt`; `gh issue view 116 --json number,title,state,body,labels,url,updatedAt`; `rg -n "TF-STATUS-022|#140|derive_charset|oneclick.derive_charset|charset_contracts|OneClickMigrationWorker|derive_oneclick_charset_contracts" ...` | PASS | Historical row: at that point `main` was aligned with `origin/main`, #140 was the next actionable in-repo issue, and #116 remained external real-Mac evidence; #140 is now closed |
| 2026-06-26 | Current main full Python suite | `pytest -q` | PASS | 1786 passed, 5 warnings |
| 2026-06-26 | Current main Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 166 lib tests, JSONL CLI test, 6 live-roundtrip tests, 2 non-ignored stress tests, doctests |
| 2026-06-26 | Current main Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Produced release Rust core binary |
| 2026-06-26 | Current main Python syntax | `python -m compileall -q main.py src tests scripts` | PASS | No compile errors |
| 2026-06-26 | Current main live UI evidence validator | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json` | PASS | 2 directions and 12,000,000 rows checked |
| 2026-06-26 | Current main Rust performance evidence validator | `python scripts\validate-rust-core-performance-evidence.py` | PASS | 4 files and 11,000,000 rows proven |
| 2026-06-26 | Current main optional evidence regression gate | `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | Requires both archived Rust performance evidence and live UI migration evidence |
| 2026-06-26 | Current main macOS support gate | `python scripts\check-macos-support-gate.py --skip-github` | PASS | Repository-side macOS support tracking checks pass without final real-Mac evidence |
| 2026-06-26 | Current main macOS focused tests | `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS | 51 passed |
| 2026-06-26 | Current main diff hygiene | `git diff --check` | PASS | No whitespace errors |
| 2026-06-26 | One-Click production-readiness audit | `tunnelforge-core service.hello`; `rg -n "oneclick\.|ONE_CLICK_MIGRATION_FEATURE_ENABLED" migration_core\src\lib.rs src tests docs README.md README.ko.md`; `gh issue view 124` | PASS | Rust Core advertises `oneclick.*` commands and Python worker uses Rust Core, but the PyQt entry point is still hidden; created GitHub #137 |
| 2026-06-26 | One-Click dry-run safety gate | `pytest tests\test_oneclick_rust_core_gate.py tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py`; `git diff --check` | PASS | Worker rejects real execution until #138; dialog locks Dry-run checked/disabled |
| 2026-06-26 | One-Click dry-run evidence | `pytest tests\test_oneclick_dry_run_evidence.py -q`; `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | Local MySQL Rust Core `oneclick.run` dry-run evidence captured and wired to optional regression gate |
| 2026-06-26 | One-Click dry-run preview gate | `pytest tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_exposes_oneclick_as_dry_run_preview_only -q`; `pytest tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_accepts_complete_report tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_requires_preview_ui_enabled -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json` | PASS | PyQt entry point is visible as dry-run preview; evidence requires preview UI enabled and real execution disabled |
| 2026-06-26 | One-Click issue split | `gh issue create` created #138; `gh issue view 137`; `gh issue view 138`; `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_rejects_real_execution_until_readiness_gate_opens tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_locks_dry_run_until_readiness_gate_opens -q`; `rg -n "TF-STATUS-019|TF-STATUS-020|#138|ONECLICK_REAL_EXECUTION_ENABLED" docs src tests migration_core` | PASS | #137 dry-run preview gate is separated from #138 real-execution/automatic-fix coverage; real-execution lock copy points to #138 |
| 2026-06-26 | One-Click real-execution evidence contract | `pytest tests\test_oneclick_real_execution_evidence.py -q` RED, then GREEN; `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json` expected reject; `python -m compileall -q scripts tests`; `git diff --check` | PASS | #138 real-execution validator and optional gate hook added; template is rejected until real git SHA/evidence is captured |
| 2026-06-26 | One-Click engine_innodb apply path | `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_actions_accepts_only_engine_innodb_steps --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_plan_executes_engine_innodb_sql --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_fixes_real_engine_innodb_requires_endpoint --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml`; `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json` expected reject; `git diff --check` | PASS | Rust Core `oneclick.apply_fixes` now executes only planned `deprecated_engine -> engine_innodb` actions through the Rust adapter path and fails closed when a real apply request lacks an endpoint |
| 2026-06-26 | One-Click real-execution evidence capture | `pytest tests\test_db_core_service.py::test_facade_uses_oneclick_apply_fixes_protocol -q` RED/GREEN; `pytest tests\test_oneclick_real_execution_capture.py -q` RED/GREEN; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-real-execution-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `pytest tests\test_oneclick_real_execution_capture.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q src\core\db_core_service.py scripts\capture-oneclick-real-execution-evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py`; `git diff --check` | PASS | Local MySQL evidence captured: Rust Core `oneclick.apply_fixes` converted `tf_oneclick_real_execution.tf_oneclick_legacy_engine_table` from `MyISAM` to `InnoDB` while app real execution stayed disabled |
| 2026-06-26 | One-Click deprecated engine live discovery | `cargo test --manifest-path migration_core\Cargo.toml oneclick_issues_classify_deprecated_engine_marker_as_auto_fixable --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml mysql_deprecated_engine_sql_targets_table_engines --lib` RED/GREEN; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo test --manifest-path migration_core\Cargo.toml`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` | PASS | MySQL inspection can now emit deprecated-engine markers for MyISAM tables and One-Click converts those markers into typed `deprecated_engine` auto-fix candidates |
| 2026-06-26 | One-Click run orchestration for engine_innodb | `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture` RED/GREEN | PASS | UI-facing Rust Core `oneclick.run dry_run=false` now sequences the validated `engine_innodb` apply path and converts a live MyISAM table to InnoDB |
| 2026-06-26 | One-Click limited real-execution PyQt gate | `pytest tests\test_oneclick_rust_core_gate.py -q` RED then GREEN; `pytest tests\test_oneclick_rust_core_gate.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json` | PASS | PyQt keeps Dry-run default, allows limited backup-confirmed real execution, and rejects non-dry-run without backup confirmation |
| 2026-06-26 | One-Click #138 closure gate | `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py src\ui\dialogs\migration_dialogs.py src\core\i18n.py scripts\validate-oneclick-dry-run-evidence.py scripts\validate-oneclick-real-execution-evidence.py tests\test_oneclick_rust_core_gate.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_real_execution_evidence.py`; `git diff --check` | PASS | #138 acceptance is satisfied for the exact `deprecated_engine -> engine_innodb` automatic scope |
| 2026-06-26 | Post-#138 open issue scan and #116 re-audit | `gh issue list --repo sanghyun-io/tunnelforge --state open --limit 20 --json number,title,labels,url`; `gh issue view 116 --repo sanghyun-io/tunnelforge --json number,title,state,body,comments,url,labels`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS | #116 is the only remaining open issue; no repository-side macOS gap found, final real operator Mac evidence is still external |
| 2026-06-26 | One-Click follow-up issue split | `rg -n "charset_issue|invalid_date|zerofill_usage|float_precision|enum_empty_value|deprecated_engine|engine_innodb|manual|oneclick_recommend|oneclick_apply" migration_core\src\lib.rs tests docs\oneclick_readiness.md`; `gh issue create` created #139 | PASS | Charset/collation One-Click automatic fix coverage is now tracked separately from closed #138 |
| 2026-06-26 | One-Click charset/collation evidence contract | `pytest tests\test_oneclick_charset_evidence.py -q` RED then GREEN; `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.template.json` expected reject; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` expected reject without completed evidence | PASS | #139 now has a machine-checkable evidence contract/template and optional regression gate hook, but no charset real execution is enabled |
| 2026-06-26 | Full Python suite | `pytest -q` | PASS | 1707 passed, 3 warnings |
| 2026-06-26 | Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | Unit, CLI, and gated live tests pass or skip according to env |
| 2026-06-26 | Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Produced `migration_core\target\release\tunnelforge-core.exe` |
| 2026-06-26 | Python syntax | `python -m compileall -q main.py src tests` | PASS | No compile errors |
| 2026-06-26 | Diff hygiene | `git diff --check` | PASS | No whitespace errors |
| 2026-06-26 | Core smoke | `tunnelforge-core service.hello` | PASS | Advertises dump/import and migration commands |
| 2026-06-26 | Live MySQL/PostgreSQL smoke | `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip -- --nocapture` | PASS | 6 live container tests passed against MySQL 8.4 on port 3406 and PostgreSQL 16 on port 55432 |
| 2026-06-26 | Live UI evidence capture tests | `pytest tests\test_live_ui_migration_capture.py tests\test_live_ui_migration_evidence.py -q` | PASS | Capture helper report shape and final validator behavior covered |
| 2026-06-26 | Live UI capture smoke | `python scripts\capture-live-ui-migration-evidence.py --rows 1000 --chunk-size 250 --seed-local-containers --output reports\live_ui_migration\live-ui-migration-evidence-smoke.json --stress-source-type synthetic_adapter --stress-peak-rss-mb 512 --stress-rss-limit-mb 2048 --stress-notes "smoke only; not #136 closure evidence"` | PASS | Smoke produced bidirectional 1,000-row worker evidence; smoke artifact removed and not used for #136 closure |
| 2026-06-26 | Live UI evidence negative check | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-smoke.json` | FAIL expected | Validator rejected the smoke report because rows were below 1,000,000 |
| 2026-06-26 | Live UI 1M partial capture | `python scripts\capture-live-ui-migration-evidence.py --rows 1000000 --chunk-size 10000 --seed-local-containers --output reports\live_ui_migration\live-ui-migration-evidence-1m-local.json --stress-source-type synthetic_adapter --stress-peak-rss-mb 0 --stress-rss-limit-mb 0 --stress-notes "placeholder; RSS not measured in this run, do not use as final #136 evidence"` | PASS | Both 1M directions migrated+verified through `CrossEngineMigrationWorker`; max heartbeat gap 125ms; renamed to `live-ui-migration-evidence-1m-local-partial.json` |
| 2026-06-26 | Live UI partial evidence negative check | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-1m-local-partial.json` | FAIL expected | Validator rejects the partial because 10M RSS fields are intentionally 0 |
| 2026-06-26 | Rust Core 10M stress RSS | `TF_STRESS_ROWS=10000000 TF_STRESS_CHUNK_SIZE=200000 TF_STRESS_RSS_REPORT=<abs>\stress-10m-rss.json TF_STRESS_RSS_LIMIT_MB=2048 cargo test --manifest-path migration_core\Cargo.toml --test stress_rss synthetic_10m_stress_resume_verify_reports_rss_bound -- --ignored --nocapture` | PASS | 10M synthetic adapter resume+verify succeeded; peak RSS 921MB / 2048MB |
| 2026-06-26 | Final live UI evidence validator | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json` | PASS | 2 directions and 12,000,000 rows checked |
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

Largest implementation files after Clean Code Round 3:

- `migration_core/src/ddl.rs` - about 2,710 lines.
- `src/ui/dialogs/sql_editor_dialog.py` - about 2,534 lines.
- `migration_core/src/migrate.rs` - about 2,491 lines.
- `migration_core/src/oneclick.rs` - about 2,125 lines.
- `migration_core/src/dump.rs` - about 2,091 lines.
- `src/ui/dialogs/db_import_dialog.py` - about 1,611 lines.
- `src/ui/dialogs/cross_engine_migration_dialog.py` - about 1,429 lines.
- `src/ui/dialogs/db_export_dialog.py` - about 1,368 lines.

Impact:

- Round 1 through Round 3 removed the worst legacy god-file hotspots, including
  the previous `db_dialogs.py` and main-window concentration. Remaining large
  files still have enough surface area to make broad behavior changes risky.

Next action:

1. Keep future fixes narrowly scoped and test-first.
2. Treat further structural splitting as watch work, not an active blocker,
   unless a nearby feature touches one of the remaining large files.

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

Status: `open`
Severity: Low
Area: macOS release readiness

Evidence:

- `docs/macos_support.md` explicitly states final real-Mac validation is
  separate from repository verification.
- 2026-06-26 update: PR #117 is merged, `python
  scripts\check-macos-support-gate.py --skip-github` passes, and focused macOS
  support tests pass locally, but GitHub issue #116 remains open because the
  final real operator Mac interactive evidence bundle is not attached.
- 2026-06-26 update: after #99/#136 closure, `python
  scripts\check-macos-support-gate.py --skip-github`, `pytest
  tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`, and
  `python -m compileall -q scripts tests` still pass on main. #116 remains open
  only for the real operator Mac report/log/system-evidence/evidence-zip
  attachment.
- 2026-06-26 current-main re-audit before #137 creation: full Python suite,
  full Rust core tests, Rust release build, compileall, final live UI evidence
  validator, Rust performance evidence validator, optional evidence regression
  gate with both required evidence flags, macOS support gate, focused macOS
  tests, and diff hygiene all pass. GitHub issue #116 still has only the final
  real operator Mac validation checkbox unchecked.
- 2026-06-27 post-#142 next issue analysis: #116 is still the only open GitHub
  issue. `python scripts\check-macos-support-gate.py` passes, but
  `python scripts\check-macos-support-gate.py --final` fails as expected
  because no macOS manual validation report was found under `build/` and no
  successful manual `macOS App Validation` `workflow_dispatch` run exists for
  the current merged main HEAD.
- 2026-07-10 fresh final-gate run at `edd0c75` confirms both conditions remain:
  no real-Mac report under `build/` and no successful manual workflow run for
  the current merged main HEAD.

Next action:

1. Do not call macOS support production-ready until the final manual validation
   evidence bundle exists.
2. Before closing #116, run the manual macOS validation flow on a real operator
   Mac from current `main`, including the signed/notarized manual workflow
   artifact run for the same merged main HEAD.

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

### TF-STATUS-013: MySQL JSON Fallback Insert Encoding

Status: `closed`
Severity: High
Area: Rust Core dump.import / MySQL INSERT fallback

Evidence:

- GitHub issue #118 reported MySQL `ERROR 3140 (22032): Invalid JSON text:
  "Invalid encoding in string."` while importing
  `ai_phase1_cache.result_json`.
- MySQL JSON fallback INSERT literals now use the `_utf8mb4` introducer so
  JSON text is interpreted with the character set required by MySQL JSON
  parsing, regardless of the connection/session default character set.
- Import session tuning now removes `NO_BACKSLASH_ESCAPES` while data is being
  loaded so JSON escape sequences generated by TunnelForge are interpreted
  consistently during fallback INSERT.
- Focused tests cover utf8mb4 JSON literal generation, preservation of JSON
  escape backslashes, and the adjusted MySQL import session tuning SQL.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/118

Impact:

- MySQL JSON columns containing non-ASCII text and escaped quotes are less
  likely to fail during safe INSERT fallback when `LOAD DATA LOCAL` is
  unavailable.

Next action:

1. Prefer live reproduction evidence if another `ERROR 3140` report appears,
   because malformed source JSON should still fail as data-invalid.

### TF-STATUS-014: Large SQL Editor Rendering

Status: `closed`
Severity: Medium
Area: SQL editor UI

Evidence:

- GitHub issue #86 reported severe slowdown when opening a large SQL file of
  roughly 645KB.
- `SQLEditorTab` now detects SQL text at or above 512KB and enables a
  large-document mode before calling `setPlainText`.
- Large-document mode detaches the syntax/validation highlighter, stops the
  validation debounce timer, and skips whole-document validation requests.
- The tab shows an inline notice that syntax highlighting and real-time
  validation are disabled for the large SQL document.
- Returning to small content re-enables the normal validator highlighter.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/86

Impact:

- Large SQL files avoid the expensive whole-document regex highlighter and SQL
  validator passes that were the dominant app-side rendering cost.

Next action:

1. Revisit true virtualized SQL rendering only if large plain-text insertion
   remains a measured bottleneck after this guard.

### TF-STATUS-015: SQL Editor Schema Tree

Status: `closed`
Severity: Medium
Area: SQL editor UI

Evidence:

- GitHub issue #92 requested a SQL editor side panel for schema/table browsing
  so users do not need to type table names manually.
- The SQL editor now has a left-side `스키마 / 테이블` tree panel next to the
  editor/results splitter.
- The tree shows DB/schema roots from the SQL editor selector and populates
  tables and columns under the currently loaded schema metadata.
- Clicking a table item inserts the table identifier into the current editor,
  quoted with backticks for MySQL and double quotes for PostgreSQL.
- Focused tests cover tree population from loaded metadata and table-click
  insertion into the editor.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/92

Impact:

- SQL editor users can discover available schemas/tables/columns in-place and
  insert table names without memorizing or manually typing them.

Next action:

1. Consider column insertion or drag/drop later if users ask for richer query
   composition.

### TF-STATUS-016: MySQL Post-Load DDL Table-Full Guidance

Status: `closed`
Severity: Medium
Area: Rust Core dump.import diagnostics

Evidence:

- GitHub issue #126 reported MySQL `ERROR 1114 (HY000): The table
  '#sql-1cbc_17b' is full` during replace import post-load DDL.
- Earlier handling classified the failure as `post_load_validation_failed` and
  included the exact failing post-load DDL statement.
- The current update adds a specific guidance suffix for MySQL table-full
  errors, telling the operator that target MySQL storage or temporary table
  space is full and to increase disk space, `tmpdir` capacity, or
  `innodb_temp_data_file_path` before retrying.
- Focused regression coverage verifies the `ERROR 1114` guidance while keeping
  the existing SQL-context classification behavior.
- GitHub issue: https://github.com/sanghyun-io/tunnelforge/issues/126

Impact:

- TunnelForge now distinguishes this class as an actionable target
  environment/resource condition instead of leaving users with a raw MySQL
  temporary table name.

Next action:

1. If another `ERROR 1114` report appears after this guidance, collect target
   MySQL storage, `tmpdir`, and InnoDB temporary tablespace evidence rather than
   changing import semantics first.

### TF-STATUS-017: Rust Core Performance Evidence Is Durable

Status: `closed`
Severity: High
Area: Rust Core migration performance evidence

Evidence:

- GitHub issue #99 requires MySQL/PostgreSQL 1M row migration+verify and 10M row
  streaming/resume/verify evidence before the Rust DB Core Service epic can be
  closed.
- `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File
  scripts\rust-core-regression-gate.ps1` passes on this machine because the
  expected performance JSONL files exist under `migration_core\target`.
- `migration_core\target` is ignored by git, so those JSONL files are local
  machine state rather than durable repo, CI, release, or handoff evidence.
- 2026-06-26 update: the four required JSONL files are archived under
  `reports\rust_core_performance`, with `README.md` documenting refresh and
  validation.
- `scripts\validate-rust-core-performance-evidence.py` validates that all four
  files exist, contain successful Rust Core `result` events, prove the required
  1M/10M row counts, and do not report verification mismatches.
- `scripts\rust-core-regression-gate.ps1` now uses the archived evidence
  validator when `RUST_CORE_REQUIRE_PERF_EVIDENCE=1`, so a clean checkout audits
  committed evidence instead of ignored `target` artifacts.
- Parent GitHub epic: https://github.com/sanghyun-io/tunnelforge/issues/99
- Follow-up GitHub issue:
  https://github.com/sanghyun-io/tunnelforge/issues/135

Impact:

- #99 can now point at repo-preserved 1M/10M performance evidence and a
  repeatable validator instead of relying on one developer's ignored local
  `target` directory.

Next action:

1. Refresh the archived evidence if Rust Core migration/verify streaming
   semantics change.

### TF-STATUS-018: Rust Core Live UI Performance Evidence Complete

Status: `closed`
Severity: High
Area: Rust Core live migration / PyQt responsiveness evidence

Evidence:

- GitHub issue #99 requires MySQL -> PostgreSQL and PostgreSQL -> MySQL 1M row
  migration+verify to complete without UI freeze.
- TF-STATUS-017 preserves Rust Core 1M/10M JSONL evidence, but those archived
  files alone do not prove bidirectional live database coverage or PyQt
  responsiveness during a live 1M migration run.
- Cross-engine worker/dialog tests cover progress, checkpoint, resume, and
  worker signal plumbing, but they do not run a live 1M row UI workflow.
- 2026-06-26 update: `scripts\validate-live-ui-migration-evidence.py` and
  `reports\live_ui_migration\live-ui-migration-evidence.template.json` now
  define the machine-checkable final evidence shape for #136.
- 2026-06-26 update: local Docker-backed MySQL 8.4 and PostgreSQL 16 endpoints
  passed the existing `live_roundtrip` Rust integration tests, covering inspect,
  readiness, guide, preflight, MySQL -> PostgreSQL migrate+verify, and
  PostgreSQL -> MySQL migrate+verify on small fixtures.
- 2026-06-26 update: `scripts\capture-live-ui-migration-evidence.py` now seeds
  local `tf-live-*` containers with deterministic `tf_live_*` tables, runs both
  directions through `CrossEngineMigrationWorker`, samples Qt event-loop
  heartbeat gaps while the migrate worker is active, and writes the
  validator-compatible report.
- 2026-06-26 update: a 1,000-row smoke run of the capture helper succeeded for
  both directions and was intentionally rejected by the final validator because
  it was below the required 1,000,000 rows.
- 2026-06-26 update: `live-ui-migration-evidence-1m-local-partial.json`
  preserves a local Docker 1M bidirectional PyQt worker run. MySQL ->
  PostgreSQL and PostgreSQL -> MySQL each migrated and verified 1,000,000 rows,
  emitted 201 worker progress events, and recorded a 125ms max Qt heartbeat gap
  against a 1000ms threshold. The file remains partial because the 10M RSS
  fields are intentionally 0 and therefore fail the final validator.
- 2026-06-26 update: `migration_core\tests\stress_rss.rs` adds an ignored
  10M synthetic adapter RSS harness. The committed harness measured 10M
  resume+verify success, 0 mismatches, and 921MB peak RSS against a 2048MB
  limit, writing `reports\live_ui_migration\stress-10m-rss.json`.
- 2026-06-26 update: `reports\live_ui_migration\live-ui-migration-evidence.json`
  combines the live bidirectional 1M PyQt worker evidence with the 10M RSS
  measurement. `python scripts\validate-live-ui-migration-evidence.py
  reports\live_ui_migration\live-ui-migration-evidence.json` passes with 2
  directions and 12,000,000 rows checked.
- 2026-06-26 update: `scripts\rust-core-regression-gate.ps1` can now require
  the live UI evidence validator when `RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1`.
- GitHub issue #136 tracked this final #99 closure evidence and is now closed.
- Parent GitHub epic: https://github.com/sanghyun-io/tunnelforge/issues/99
- Follow-up GitHub issue:
  https://github.com/sanghyun-io/tunnelforge/issues/136

Impact:

- #99/#136 now have durable validator-passing evidence for live bidirectional
  1M PyQt worker responsiveness and 10M stress RSS bounds.
- Keep the final validator in the release evidence path if migration worker,
  Rust Core migration streaming, or stress adapter semantics change.

Next action:

1. Refresh `reports\live_ui_migration\live-ui-migration-evidence.json` only if
   migration worker, Rust Core streaming, heartbeat sampling, or stress/RSS
   semantics change.

### TF-STATUS-019: One-Click Migration UI Dry-Run Preview Gate

Status: `closed`
Severity: Medium
Area: One-Click migration UI / Rust Core integration

Evidence:

- GitHub issue #124 is closed because One-Click migration orchestration moved
  into Rust Core, but its acceptance criteria intentionally kept the hidden UI
  gate disabled until the workflow is production-ready.
- `tunnelforge-core service.hello` advertises `oneclick.run`,
  `oneclick.preflight`, `oneclick.analyze`, `oneclick.recommend`,
  `oneclick.apply_fixes`, `oneclick.validate`, and `oneclick.report`.
- `src\ui\dialogs\oneclick_migration_dialog.py` uses
  `DbCoreFacade.run_oneclick(...)` and fails closed unless the connector has
  the Rust Core facade shape.
- `src\ui\dialogs\migration_dialogs.py` sets
  `ONE_CLICK_MIGRATION_FEATURE_ENABLED = True`, exposing the entry point as
  "One-Click Dry-run Preview" only.
- 2026-06-26 update: created GitHub issue #137 to track the production-readiness
  decision and evidence required before changing the feature flag.
- 2026-06-26 update: `OneClickMigrationWorker` now rejects non-dry-run payloads
  while `ONECLICK_REAL_EXECUTION_ENABLED = False`, and the dialog locks the
  Dry-run checkbox checked/disabled until the real-execution gate is complete.
- 2026-06-26 update: `docs\oneclick_readiness.md` defines the current
  dry-run-only preview support scope; `reports\oneclick_readiness` now contains
  validator-backed local MySQL Rust Core `oneclick.run` dry-run evidence.
- `scripts\validate-oneclick-dry-run-evidence.py` verifies that the evidence
  includes all `oneclick.*` service capabilities, preview UI enabled,
  real-execution disabled, `dry_run=true`, every expected phase, a 100%
  progress event, zero validation remnants, and the explicit dry-run execution
  log.
- `scripts\rust-core-regression-gate.ps1` can require the evidence when
  `RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE=1`.
- 2026-06-26 follow-up analysis in `docs\oneclick_readiness.md` concluded that
  the then-current backend supported hidden or dry-run preview scope only, not
  full enablement.
- 2026-06-26 update: the migration analyzer now exposes the entry point as
  `One-Click Dry-run Preview`, with tooltip copy that says no real changes are
  performed and automatic SQL fixes are not enabled.

GitHub issue:

- https://github.com/sanghyun-io/tunnelforge/issues/137

Impact:

- Users can run the Rust Core One-Click flow as a dry-run preview from the
  migration analyzer.
- Real execution and automatic SQL fix claims remain out of scope for this
  closed dry-run preview gate.

Closure evidence:

1. Commit `40cc5ca` exposed `One-Click Dry-run Preview`, refreshed
   machine-checkable dry-run evidence, and kept real execution disabled.
2. Follow-up real-execution work was split to GitHub #138.

### TF-STATUS-020: One-Click Real Execution / Automatic Fix Coverage

Status: `closed`
Severity: High
Area: One-Click migration UI / Rust Core automatic fixes

Evidence:

- GitHub #138 tracked the remaining scope after #137: define, implement, and
  prove the automatic fix classes before real One-Click execution is enabled.
- Current Rust Core recommendation behavior marks `deprecated_engine` payload
  issues with `table_name` as automatic candidates using `engine_innodb`
  recommendation metadata. `oneclick.apply_fixes` can execute only those
  planned `engine_innodb` actions through Rust Core `MigrationAdapter`; other
  issue classes remain manual/skipped or blocked as disallowed.
- `scripts\validate-oneclick-real-execution-evidence.py` defines the
  machine-checkable #138 evidence contract for a controlled local
  `deprecated_engine -> engine_innodb` non-dry-run proof. It requires a safe
  `tf_oneclick_` schema, app real execution still disabled, all `oneclick.*`
  service capabilities, no disallowed fix attempts, and before/after table
  engine evidence proving only the allowed fix was applied.
- `reports\oneclick_readiness\oneclick-real-execution-evidence.template.json`
  documents the required evidence shape.
- `reports\oneclick_readiness\oneclick-real-execution-evidence.json` now
  contains validator-backed local MySQL evidence proving Rust Core
  `oneclick.apply_fixes` converted
  `tf_oneclick_real_execution.tf_oneclick_legacy_engine_table` from `MyISAM`
  to `InnoDB` with `ONECLICK_REAL_EXECUTION_ENABLED = False`.
- MySQL live inspection now emits deprecated-engine markers for MyISAM base
  tables, and One-Click converts those markers into typed
  `deprecated_engine` issues that can be recommended as `engine_innodb`.
- The UI-facing Rust command `oneclick.run dry_run=false` sequences the
  validated `engine_innodb` apply path.
- `src\ui\dialogs\oneclick_migration_dialog.py` now keeps
  `ONECLICK_REAL_EXECUTION_ENABLED = True`, leaves Dry-run checked by default,
  and fails closed when a non-dry-run payload lacks backup confirmation.
- `src\ui\dialogs\migration_dialogs.py` now exposes `One-Click Migration` with
  user-facing copy that says Dry-run is the default and the only automatic
  non-dry-run scope is verified MyISAM/deprecated engine tables becoming
  `InnoDB` after backup confirmation.

GitHub issue:

- https://github.com/sanghyun-io/tunnelforge/issues/138

Impact:

- Users can run dry-run inspection by default.
- Users can opt into non-dry-run only after backup confirmation, and Rust Core
  applies only the validated `deprecated_engine -> engine_innodb` strategy.
- Automatic remediation for every other issue class remains out of scope and
  must be tracked separately.

Closure evidence:

1. Rust Core contract tests, local MySQL before/after evidence, and the
   machine-checkable real-execution validator all pass for
   `deprecated_engine -> engine_innodb`.
2. PyQt tests prove the dialog keeps Dry-run default, allows limited
   non-dry-run with backup confirmation, and rejects non-dry-run without backup
   confirmation.
3. Docs and user-facing copy document the exact automatic/manual split.

Next action:

1. Create separate issues for any additional automatic fix class before
   enabling it.
2. Keep production database usage out of One-Click real execution until there
   is explicit production-readiness evidence.

### TF-STATUS-021: One-Click Charset/Collation Automatic Fix Coverage

Status: `closed`
Severity: High
Area: One-Click migration UI / Rust Core automatic fixes

Evidence:

- GitHub #139 tracks charset/collation automatic fix coverage as a separate
  follow-up after #138.
- `docs\oneclick_readiness.md` now limits `charset_issue` automation to
  supplied complete `charset_collation_fk_safe` contracts with explicit target,
  FK order, rollback SQL, and local-safe evidence.
- Rust Core One-Click apply logic allowlists `deprecated_engine -> engine_innodb`
  plus the supplied complete `charset_issue -> charset_collation_fk_safe`
  contract shape; missing or incomplete charset data remains manual/fail-closed.
- `scripts\validate-oneclick-charset-evidence.py` and
  `reports\oneclick_readiness\oneclick-charset-evidence.template.json` now
  define the required #139 evidence shape. The validator requires local MySQL
  source, safe `tf_oneclick_` schema/table identifiers, explicit
  `utf8mb4`/collation target proof, FK-valid after-state, rollback metadata,
  and zero disallowed fix attempts.
- `scripts\rust-core-regression-gate.ps1` can require completed charset
  evidence with `RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE=1`; it fails
  until `reports\oneclick_readiness\oneclick-charset-evidence.json` exists.
- `docs\oneclick_readiness.md` now defines the #139 policy boundary: only
  table-level `charset_issue -> charset_collation_fk_safe` with explicit
  target charset/collation, FK closure/order evidence, rollback metadata, and
  local `tf_oneclick_` evidence can become automatic in a future change.
- 2026-06-26 historical next-issue analysis selected #139 as the next
  in-repo issue after that `main` merge. GitHub #116 remained external real-Mac
  evidence work, while #139 had concrete Rust Core, PyQt, and local MySQL
  evidence tasks that could proceed in this repository. #139 is now closed.
- Existing Python Fix Wizard charset code already generates FK DROP, table
  conversion, FK ADD, and recovery SQL, but #139 must not route One-Click real
  execution through Python DB drivers. The reusable idea is the contract shape;
  execution ownership must stay in `tunnelforge-core`.
- `scripts\capture-oneclick-charset-evidence.py` and
  `tests\test_oneclick_charset_capture.py` now implement the #139 local MySQL
  capture/report layer through Rust DB Core APIs. The helper seeds only safe
  `tf_oneclick_` scopes, captures before/after charset state, captures FK
  evidence, executes `oneclick.apply_fixes dry_run=false`, and writes a
  validator-compatible report.
- `migration_core\src\lib.rs` has a `charset_collation_fk_safe` contract helper
  covered by Rust tests. It validates safe `tf_oneclick_` evidence identifiers,
  explicit charset/collation target, FK order table coverage, rollback SQL, and
  generated table-level conversion SQL.
- Rust Core now gates `charset_issue -> charset_collation_fk_safe`
  recommendations on complete request `charset_contracts[]` evidence and keeps
  charset issues manual when that evidence is missing or incomplete.
  `oneclick.apply_fixes dry_run=true` can preview charset `planned_fixes` from
  the same contract.
- Rust Core command-level `oneclick.apply_fixes dry_run=false` can now execute
  complete `charset_collation_fk_safe` contract SQL through the adapter path and
  returns rollback metadata, target charset/collation, FK order, SQL list, and
  success/error state in `applied_fixes`.
- `reports\oneclick_readiness\oneclick-charset-evidence.json` now provides
  validator-backed local MySQL evidence for the command-level charset path.
  `RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE=1` passes.
- UI-facing Rust Core `oneclick.run dry_run=false` now merges supplied
  `issues[]` and `charset_contracts[]`, shifts charset contract indexes behind
  inspection-derived issues, and executes the same allowlisted complete
  `charset_collation_fk_safe` contract shape. A live MySQL regression proves
  FK-connected local `tf_oneclick_` tables convert from `utf8mb3` /
  `utf8mb3_general_ci` to `utf8mb4` / `utf8mb4_0900_ai_ci`.
- PyQt rendering/count/copy coverage now proves charset automatic, manual, and
  skip payloads are counted and logged accurately.
- GitHub #140 / TF-STATUS-022 is closed after local PyQt-triggered derivation
  evidence proved `OneClickMigrationWorker._core_payload()` feeds derived
  `issues[]` / `charset_contracts[]` into `oneclick.run dry_run=false`.

GitHub issue:

- https://github.com/sanghyun-io/tunnelforge/issues/139

Impact:

- Users can still run One-Click dry-run and the validated engine fix. Charset
  execution is available only for complete local-safe Rust Core contracts,
  including contracts derived by Rust Core for the PyQt worker path in local
  `tf_oneclick_` evidence scopes.
- Production charset/collation execution remains out of scope without separate
  production-readiness evidence.

Next action:

1. Keep #139 evidence refreshed if the supplied charset contract, validator, or
   One-Click event payload changes.
2. Keep #140 derivation evidence refreshed if PyQt payload construction,
   Rust Core derivation, or One-Click event payloads change.

### TF-STATUS-024: Direct DB Export/Import Uses Connector Host

Status: closed
Severity: High
Area: Export/Import UI

Evidence:

- 2026-06-26 audit found `RustDumpExportDialog.do_export()` and
  `RustDumpImportDialog.do_import()` created `RustDumpConfig` with
  `host="127.0.0.1"` even when the active connector represented a direct
  remote DB connection.
- Tunnel flows normally expose `connector.host == "127.0.0.1"` because they
  connect through a local forwarded port, but direct flows must preserve the
  connector host.
- RED/GREEN tests now cover both dialogs with a direct connector at
  `db.example.com:3307`.

Resolution:

- Export and Import dialogs now set `RustDumpConfig.host` from
  `connector.host`, falling back to `127.0.0.1` only when the connector lacks a
  host attribute.
- Focused tests verify host, port, user, and password are forwarded to the Rust
  DB Core worker config.

Next action:

1. Keep direct-connection endpoint coverage aligned if Export/Import worker
   construction moves or if PostgreSQL dump support is added later.

### TF-STATUS-025: macOS Artifact Lookup Uses Current Main After PR Merge

Status: closed
Severity: High
Area: macOS release validation

Evidence:

- 2026-06-27 next-issue analysis found `scripts/macos-download-validation-artifacts.sh`
  still defaulted to the latest successful manual `macOS App Validation` run
  for `PR #117 head`.
- That conflicted with the already-fixed final report gate policy:
  `scripts/check-macos-support-gate.py` expects report Git SHA to match PR head
  before merge, or current merged main HEAD after PR #117 is merged.
- RED/GREEN coverage now simulates merged PR #117 with a fake `gh` binary and
  fails unless the artifact lookup query uses local `git rev-parse HEAD`
  instead of the stale PR head.

Resolution:

- `scripts/macos-download-validation-artifacts.sh` now resolves the default
  artifact run target as PR head before merge, or current merged main HEAD after
  PR #117 is merged.
- `scripts/macos-manual-validation-report.sh` and `docs/macos_support.md`
  describe the same default, keeping operator instructions aligned with the
  final gate.

Next action:

1. Keep the artifact lookup default aligned with
   `check-macos-support-gate.py::expected_final_report_sha` if the final
   validation branch/merge policy changes.
2. #116 still requires real operator Mac validation evidence before it can be
   closed.

### TF-STATUS-026: Schedule Guide Stays Internal While Feature Is Hidden

Status: closed
Severity: Medium
Area: Docs/UI feature flags

Evidence:

- 2026-06-27 stale-doc scan found that `SCHEDULE.md` opened with the correct
  hidden-feature warning, but later still told readers to click the toolbar
  schedule button, use the backup log tab, and run schedules immediately as if
  the feature were public.
- `src/ui/main_window.py` still sets `SCHEDULE_FEATURE_ENABLED = False`, so
  those instructions were not reachable in normal builds.
- RED/GREEN coverage in `tests/test_schedule_docs.py` now rejects public-UI
  wording while the guide is marked disabled/internal.

Resolution:

- `SCHEDULE.md` is now titled and worded as an internal implementation /
  reactivation memo.
- Current-user steps were converted into reactivation verification items for
  entry point, create/save, immediate run, logs, and app lifecycle behavior.

Next action:

1. If `SCHEDULE_FEATURE_ENABLED` is intentionally enabled, rewrite
   `SCHEDULE.md` back into a public user guide and add fresh UI/runtime
   verification evidence in the same session.

### TF-STATUS-027: One-Click Production Scope Wording Matches Limited Gate

Status: closed
Severity: Medium
Area: One-Click migration docs

Evidence:

- 2026-06-27 repo-side scan found `docs/oneclick_readiness.md` still said
  `Production database usage` was not supported, even though the current UI
  gate allows backup-confirmed non-dry-run execution for the validated
  `deprecated_engine -> engine_innodb` path.
- The same document already stated `ONECLICK_REAL_EXECUTION_ENABLED = True`,
  Dry-run default, backup confirmation requirement, and limited
  `engine_innodb` execution, so the old production-usage bullet was stale
  scope wording rather than the active implementation policy.
- RED/GREEN coverage now rejects the broad stale bullet and requires the docs
  to distinguish backup-confirmed `engine_innodb` from unsupported broad
  production automatic remediation and production charset/collation execution.

Resolution:

- `docs/oneclick_readiness.md` now states that broad production automatic
  remediation is unsupported, while the only current non-dry-run
  production-facing path is backup-confirmed
  `deprecated_engine -> engine_innodb`.
- Production charset/collation execution remains explicitly unsupported.

Next action:

1. Keep this wording aligned if the One-Click real-execution allowlist expands
   beyond `engine_innodb`.

### TF-STATUS-028: Current Full Python Suite Count Refreshed

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- 2026-06-27 `pytest -q` completed with `1827 passed, 5 warnings`.
- `docs/current_status.md` still reported the previous `1786 passed, 5
  warnings` count from before the added documentation regression tests.
- RED/GREEN coverage now rejects the stale `1786 passed` line and requires the
  current `1827 passed, 5 warnings` evidence.

Resolution:

- The top `pytest -q` verification row now reports `1827 passed, 5 warnings`.
- The verification log records the exact full-suite refresh command.

Next action:

1. Refresh the count again whenever new tests are added and a full `pytest -q`
   run is completed.

### TF-STATUS-029: Current Baseline Verification Heading Is Not Stale-Dated

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- 2026-06-27 status audit found the top verification table still used
  `## Verified On 2026-06-26` even after its `pytest -q` row had been refreshed
  with 2026-06-27 evidence.
- That heading made the mixed baseline ambiguous: the full Python suite count
  was current, while broader Rust/macOS rows were preserved from the 2026-06-26
  sweep.
- RED/GREEN coverage now rejects the stale-dated heading and requires explicit
  wording that the full-suite count was refreshed on 2026-06-27.

Resolution:

- The section is now `## Current Baseline Verification`.
- The paragraph under the heading states which evidence was refreshed on
  2026-06-27 and which broader baseline rows are preserved until rerun.

Next action:

1. If a full broad baseline sweep is rerun, replace the preservation note with
   that sweep's concrete date and command evidence.

### TF-STATUS-030: Current Main Next-Issue Re-Audit

Status: closed
Severity: Low
Area: Status documentation / Rust Core boundary audit

Evidence:

- 2026-06-27 main alignment check found `main` aligned with `origin/main`;
  latest pushed commits already include the recent schedule, One-Click, and
  status documentation fixes.
- GitHub issue scan found #116 as the only open issue.
- `python scripts\check-macos-support-gate.py --skip-github` passed.
- `python scripts\check-macos-support-gate.py` passed against GitHub state,
  confirming #110-#115 closure, #116/M6 tracking, merged PR #117 state, and
  green repository-side checks.
- `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`
  passed with 53 tests.
- Rust Core boundary scan checked DB driver/tool names, subprocess callers,
  SQL cursor/commit/rollback usage, and disabled feature flags. The scan found
  no new repo-side Rust Core baseline violation: legacy-shaped
  `MySQLConnector` and `PostgresConnector` now open `DbCoreFacade`
  connections and expose `RustDbConnection`/`RustDbCursor` shims, while hidden
  scheduler SQL execution uses `create_rust_db_connector`.

Resolution:

- No new GitHub issue was created from this pass because the only remaining
  actionable item is already tracked as #116 / TF-STATUS-008 and requires
  external real-Mac operator evidence.
- The re-audit is recorded here so later sessions do not repeat the same
  connector-name false positive without new evidence.

Next action:

1. Keep #116 open until a real Mac operator attaches the completed evidence
   bundle and final handoff comment.
2. If a future scan finds an actual non-Rust DB operation owner path, create a
   separate GitHub issue before implementation.

### TF-STATUS-031: Current Baseline Counts After Re-Audit Coverage

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- Adding the TF-STATUS-030 current-status regression test changed the full
  Python suite size.
- The top `Current Baseline Verification` macOS focused row still preserved
  `51 passed` even though the current 2026-06-27 focused run is `52 passed`.
  Later TF-STATUS-038 coverage superseded this again to `53 passed`.
- RED/GREEN coverage now rejects `PASS, 51 passed` inside the current baseline
  section and rejects the previous 1793-test full-suite count as the current
  pytest row.

Resolution:

- The top `pytest -q` verification row now reports the current full-suite count.
- The top macOS focused verification row now reports the current focused count.
- The focused verification table records the refreshed full-suite command.

Next action:

1. Refresh these counts whenever tests are added and the matching verification
   commands are rerun.

### TF-STATUS-032: Focused Verification Table Has No Duplicate Check Rows

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- The `Focused Verification On 2026-06-27` table listed
  `python scripts\check-macos-support-gate.py --skip-github` twice.
- RED/GREEN coverage now extracts focused verification command rows and rejects
  duplicate command entries.

Resolution:

- Removed the duplicate focused verification row while preserving the full
  #116 gate and skip-GitHub gate evidence.

Next action:

1. Keep focused verification tables deduplicated when adding future evidence
   rows.

### TF-STATUS-033: Current Baseline Table Has No Duplicate Check Rows

Status: closed
Severity: Low
Area: Status documentation

Evidence:

- The `Current Baseline Verification` table listed `tunnelforge-core
  service.hello` twice: once for dump/import/migration capability evidence and
  once for One-Click capability evidence.
- RED/GREEN coverage now extracts current baseline check rows and rejects
  duplicate command entries.

Resolution:

- Merged the duplicate `service.hello` rows into one row covering dump/import,
  migration, and One-Click command advertisement.

Next action:

1. Keep current baseline command rows unique; add detail to the result cell
   rather than duplicating a command row.

### TF-STATUS-034: Rust Core Export/Import Menu Wording

Status: closed
Severity: Low
Area: Export/Import UI

Evidence:

- The tunnel context menu still used legacy shell-branded action labels and
  handler names even though Export/Import now routes through Rust DB Core.
- RED/GREEN coverage now scans `src/ui/main_window.py` and rejects the legacy
  labels/handlers while requiring Rust DB Core action labels and handler names.

Resolution:

- The tree export/import shortcuts now dispatch to `_context_rust_core_export`
  and `_context_rust_core_import`.
- The tunnel context menu now shows `Rust DB Core Export` and
  `Rust DB Core Import`.
- The focused regression is recorded in
  `tests/test_main_window_export_import_labels.py`.

Next action:

1. Keep user-facing Export/Import wording aligned with Rust Core ownership when
   adding new context-menu or toolbar actions.

### TF-STATUS-035: One-Click Fallback Dry-Run Tooltip

Status: closed
Severity: Low
Area: One-Click migration UI

Evidence:

- `ONECLICK_REAL_EXECUTION_ENABLED` is currently true, but the disabled fallback
  tooltip still described real execution as blocked until GitHub #138 completed.
- GitHub #138 is already closed, and the current supported state is limited
  backup-confirmed real execution with dry-run as the default.
- RED/GREEN coverage now forces the disabled fallback tooltip to avoid closed
  issue wording and to state that real execution is disabled in this build.

Resolution:

- The disabled fallback tooltip now says One-Click real execution is
  `disabled in this build` and that dry-run remains available for Rust Core
  recommendation previews.
- The regression is recorded in `tests/test_oneclick_rust_core_gate.py`.

Next action:

1. Keep fallback/feature-flag copy aligned with the current One-Click support
   matrix when flags change.

### TF-STATUS-036: One-Click Module Scope Docstring

Status: closed
Severity: Low
Area: One-Click migration UI

Evidence:

- The module docstring in `src/ui/dialogs/oneclick_migration_dialog.py` still
  said the whole migration process is automatically executed.
- Current behavior is narrower: Rust DB Core owns the workflow, dry-run is the
  default, execution pauses for plan confirmation, and non-dry-run changes
  require backup confirmation with validated limited scope.
- RED/GREEN coverage now rejects the overbroad automatic-execution phrase and
  requires Rust DB Core dry-run default and limited real execution wording.

Resolution:

- Reworded the module docstring to describe Rust DB Core ownership, dry-run
  default, and backup-confirmed limited real execution.
- The regression is recorded in `tests/test_oneclick_rust_core_gate.py`.

Next action:

1. Keep One-Click source-level comments aligned with the supported execution
   matrix when the workflow expands.

### TF-STATUS-037: BUILD Installer Version Examples

Status: closed
Severity: Low
Area: Build documentation

Evidence:

- `BUILD.md` still showed stale 1.0.0 installer filename/AppVersion examples
  even though release version sources are aligned at `2.1.7` and the installer
  uses `MyAppVersion`.
- RED/GREEN coverage now rejects the stale installer example version and
  requires `{version}` / `{#MyAppVersion}` placeholders.

Resolution:

- Replaced stale Windows installer output/test examples with
  `TunnelForge-Setup-{version}.exe`.
- Updated the Inno Setup snippet to use `AppVersion={#MyAppVersion}` and note
  that it is synced from `src/version.py`.
- The regression is recorded in `tests/test_build_docs.py`.

Next action:

1. Keep build documentation examples version-neutral unless they intentionally
   document the current release version.

### TF-STATUS-038: macOS Manual Workflow Head Policy

Status: closed
Severity: High
Area: macOS release validation

Evidence:

- `scripts/macos-download-validation-artifacts.sh` already used PR head before
  merge, or current merged main HEAD after PR #117 has merged.
- `scripts/check-macos-support-gate.py` still resolved the successful manual
  `workflow_dispatch` `macOS App Validation` run from PR #117 head only.
- RED/GREEN coverage now proves merged-PR finalization uses local HEAD when
  matching the manual workflow artifact run.
- GitHub #116 now describes the manual workflow run policy as PR head before
  merge, or current merged main HEAD after PR #117 has merged.

Resolution:

- `check_manual_macos_validation_workflow()` now uses the same head policy as
  final report Git SHA and artifact download.
- `docs/macos_support.md` documents that the manual workflow_dispatch artifact
  run follows the same head policy.
- The regression is recorded in `tests/test_rust_core_packaging.py`.

Next action:

1. Keep final report Git SHA, artifact download head SHA, and manual workflow
   artifact run head SHA aligned whenever the #116 final gate changes.

### TF-STATUS-039: Post-Merge Next-Issue External Re-Audit

Status: closed
Severity: Low
Area: Status documentation / Rust Core boundary audit

Evidence:

- `git status --short --branch` shows `main` aligned with `origin/main`.
- `gh issue list --state open --limit 30` then returned only GitHub #116.
- `python scripts\check-macos-support-gate.py --skip-github` and
  `python scripts\check-macos-support-gate.py` both pass.
- Focused scans checked stale handoff wording, feature flags, direct DB driver
  paths, and SQL execution surfaces. SQL editor query execution also routes
  through the Rust connector shim via `create_sql_editor_connector()` and
  `create_rust_db_connector()`.

Resolution:

- No repo-side follow-up issue was confirmed during that pass. Therefore, no
  new GitHub issue was created from that pass; #116 remained blocked on
  external real-Mac operator validation evidence.
- This entry records the latest post-merge re-audit so future sessions do not
  re-open the SQL editor or legacy connector names as false positives without
  new evidence.

Next action:

1. Keep #116 open until the real-Mac evidence bundle is attached and the final
   device validation checkbox is checked.
2. A later scan did find confirmed repo-side evidence and created GitHub #142 /
   TF-STATUS-040.

### TF-STATUS-040: Legacy Python Auto-Fix Wizard Mutations

Status: closed
Severity: High
Area: Rust Core baseline / Migration Auto-Fix Wizard

Evidence:

- GitHub #142 tracked this issue separately from external macOS #116.
- `src/ui/dialogs/migration_dialogs.py` exposes `btn_auto_fix` and opens
  `FixWizardDialog` for auto-fixable migration issues.
- Before the fix, `src/ui/dialogs/fix_wizard_dialog.py` wired the final
  execution button to `FixWizardWorker(..., dry_run=False)`.
- Before the fix, `src/ui/workers/fix_wizard_worker.py` could call
  `FKSafeCharsetChanger.execute_safe_charset_change(..., dry_run=False)` and
  `BatchFixExecutor.execute_batch(..., dry_run=False)`.
- `src/core/migration_fix_wizard.py` directly generates and executes DDL/DML
  through `connector.connection.cursor().execute(...)`, `commit()`, and
  `rollback()`; this code remains available only behind the now fail-closed
  legacy worker path for SQL generation/dry-run behavior.
- This differs from the current baseline where `tunnelforge-core` should own
  DB mutation operations and Python/PyQt should orchestrate UI/signals/dialogs.
- The fix adds `tests/test_fix_wizard_dialog.py` coverage proving the legacy
  UI starts `FixWizardWorker` with `dry_run=True` and the worker rejects
  `dry_run=False`.

Resolution:

- No user-visible legacy Auto-Fix Wizard path can execute DB mutations through
  Python-owned fix logic.
- `ExecutionPage` is now labeled as SQL/Dry-run confirmation, calls
  `FixWizardWorker(..., dry_run=True)`, and explains that real DB changes must
  use a Rust Core-owned path.
- `FixWizardWorker` raises `RuntimeError` if constructed with `dry_run=False`,
  keeping the legacy mutation path fail-closed even if a caller bypasses the
  wizard UI.
- This remains separate from One-Click `oneclick.*`, whose current limited
  real-execution path is already Rust Core-owned and evidence-backed.

Next action:

1. Keep the legacy wizard dry-run/manual SQL only unless a future issue adds a
   Rust Core-owned command for this exact automatic-fix workflow.
2. Keep the fail-closed worker coverage when refactoring the wizard.
3. Track any future real execution path as a separate issue with Rust command
   tests before enabling it in PyQt.

### TF-STATUS-041: Legacy Auto-Fix Core Mutation APIs

Status: closed
Severity: High
Area: Rust Core baseline / Migration Auto-Fix Wizard

Evidence:

- GitHub #143 tracked the deeper follow-up after #142: the UI/worker path was
  dry-run/manual SQL only, but the underlying legacy Python core APIs still
  accepted `dry_run=False`.
- `BatchFixExecutor.execute_batch(..., dry_run=False)` could enter session
  state changes, SQL mode changes, FK check toggles, rollback capture, and
  `_execute_single(...)`.
- `FKSafeCharsetChanger.execute_safe_charset_change(..., dry_run=False)` could
  generate and execute FK DROP, ALTER, FK ADD, commit, rollback, and recovery
  SQL from Python-owned logic.
- RED/GREEN coverage in `tests/test_migration_fix_wizard.py` now proves both
  core APIs reject `dry_run=False` with a Rust Core ownership error, that
  `BatchFixExecutor` rejects mutation mode before session state or execution
  hooks are touched, and that `BatchFixExecutor._execute_single` is also
  fail-closed if called directly.
- Direct `cursor.execute`/`commit`/`rollback` mutation calls no longer appear in
  `src/core/migration_fix_wizard.py`.

Resolution:

- `BatchFixExecutor.execute_batch` raises `RuntimeError` immediately when
  `dry_run=False`.
- `FKSafeCharsetChanger.execute_safe_charset_change` raises `RuntimeError`
  immediately when `dry_run=False`.
- `BatchFixExecutor._execute_single` raises `RuntimeError` immediately if a
  direct caller tries to use the old private SQL execution hook.
- Dead legacy direct `cursor.execute`/`commit`/`rollback` bodies were removed
  from `src/core/migration_fix_wizard.py`.
- Dry-run/SQL generation remains available for preview and manual execution
  guidance.
- The older Python mutation-specific session/fallback tests were rewritten to
  assert fail-closed behavior rather than preserving an execution path that
  violates the Rust Core baseline.

Next action:

1. Keep these core APIs dry-run/SQL-generation only unless a future issue adds
   a Rust Core-owned command for the exact automatic-fix workflow.
2. If real automatic fix execution is needed later, implement it in
   `tunnelforge-core` first and add Rust command tests before exposing it in
   PyQt.

### TF-STATUS-042: Legacy MigrationAnalyzer Cleanup Mutations

Status: closed
Severity: High
Area: Rust Core baseline / Migration Analyzer cleanup

Evidence:

- GitHub #144 tracked this issue separately after #143: the legacy Auto-Fix
  core APIs were fail-closed, but `MigrationAnalyzer.execute_cleanup(...,
  dry_run=False)` still used Python-owned cursor execution.
- Before the fix, `src/core/migration_analyzer.py` opened
  `connector.connection.cursor()`, executed generated cleanup SQL, and called
  `commit()` / `rollback()` in non-dry-run cleanup mode.
- `src/ui/workers/migration_worker.py::CleanupWorker` can call
  `MigrationAnalyzer.execute_cleanup`, and
  `src/ui/dialogs/migration_dialogs.py` enabled the actual cleanup execution
  button when orphan rows existed.
- RED/GREEN coverage now proves `MigrationAnalyzer.execute_cleanup(...,
  dry_run=False)` raises a Rust Core ownership error before any cursor,
  commit, rollback, or connector query work is touched.
- UI coverage now proves the migration analyzer dialog keeps legacy actual
  cleanup execution disabled even when orphan rows exist.

Resolution:

- `MigrationAnalyzer.execute_cleanup` raises `RuntimeError` immediately when
  `dry_run=False`.
- The old direct cleanup `cursor.execute` / `commit` / `rollback` body was
  removed from `src/core/migration_analyzer.py`.
- The migration analyzer dialog describes cleanup as Dry-Run/SQL preview only,
  keeps `btn_execute` disabled, and guards direct `execute_cleanup(False)`
  calls with an explanatory warning.
- Dry-Run and SQL preview remain available.

Next action:

1. Keep legacy migration analyzer cleanup dry-run/SQL-preview only unless a
   future issue adds a Rust Core-owned cleanup command.
2. If real orphan cleanup execution is needed later, implement it in
   `tunnelforge-core` first and add Rust command tests before enabling the PyQt
   execution button.

### TF-STATUS-043: Legacy CleanupWorker Actual Cleanup Mode

Status: closed
Severity: Medium
Area: Rust Core baseline / Migration Analyzer cleanup worker

Evidence:

- GitHub #145 tracked the worker-level follow-up after #144: core cleanup
  mutation mode and the dialog were fail-closed, but `CleanupWorker` still
  accepted `dry_run=False`.
- Before the fix, a direct caller could construct `CleanupWorker(...,
  dry_run=False)`, start the thread, and receive `[실행]` progress text before
  the analyzer-level RuntimeError ended the worker.
- This did not re-enable DB mutation after #144, but it left the worker
  contract weaker and more misleading than the Rust Core fail-closed baseline.
- RED/GREEN coverage in `tests/test_migration_worker.py` now proves
  `CleanupWorker(..., dry_run=False)` rejects with a Rust Core ownership error
  at construction time.

Resolution:

- `CleanupWorker.__init__` raises `RuntimeError` immediately when
  `dry_run=False`.
- Dry-run cleanup worker construction remains available.

Next action:

1. Keep cleanup worker actual execution disabled unless a future issue adds a
   Rust Core-owned cleanup command and rewires this worker explicitly.

### TF-STATUS-044: Legacy MySQLConnector Execute Many Mutation Helper

Status: closed
Severity: Medium
Area: Rust Core baseline / DB connector helper API

Evidence:

- GitHub #146 tracked this unused connector-surface follow-up after #145.
- `src/core/db_connector.py::MySQLConnector.execute_many(...)` accepted
  arbitrary batch SQL/data, opened a cursor, called `executemany`, and
  committed from Python.
- `rg` found no repo callers of `execute_many`, so this was dormant API surface
  rather than an active feature workflow.
- RED/GREEN coverage in `tests/test_db_connector.py` now proves
  `MySQLConnector.execute_many` rejects with a Rust Core ownership error before
  cursor or commit work is touched.

Resolution:

- `MySQLConnector.execute_many` raises `RuntimeError` immediately.
- The dead direct `executemany` / `commit` body was removed from
  `src/core/db_connector.py`.
- Existing read/query helper behavior is unchanged.

Next action:

1. If batch mutation support is needed later, implement the specific workflow
   as a Rust Core command instead of reviving a generic Python `execute_many`
   helper.

## Issue Tracker

| ID | Severity | Status | Area | Short Title | Next Action |
| --- | --- | --- | --- | --- | --- |
| TF-STATUS-001 | High | closed | Export/Import Recovery | Initial import intent and strictness gates | Keep regression coverage aligned with import intent changes |
| TF-STATUS-002 | High | closed | Rust Core import | Import success gated by row verification | Keep row verification/report coverage aligned with import mode changes |
| TF-STATUS-003 | High | closed | Import UI | Object restoration wording | Keep focused regression |
| TF-STATUS-004 | High | closed | Rust Core export | Export consistency explicit | Keep metadata coverage aligned with export scheduling changes |
| TF-STATUS-005 | Medium | closed | Docs/UI flags | Disabled UI features labeled | Reverify docs if feature flags change |
| TF-STATUS-006 | Medium | watch | Maintainability | Remaining large files after Clean Code Round 3 | Keep future fixes narrow; split further only when nearby work justifies it |
| TF-STATUS-007 | Low | closed | Reporting | Referenced HTML report exists | Keep report aligned with future recovery changes |
| TF-STATUS-008 | Low | open | macOS | Current-HEAD workflow and final real-Mac validation pending | Run the manual workflow on the frozen release candidate, collect the real-Mac evidence bundle, and require the final gate to pass before production-ready claims |
| TF-STATUS-009 | High | closed | Rust Core import | Merge import post-load DDL policy | Keep merge/recreate policy tests |
| TF-STATUS-010 | High | closed | Rust Core import | Shadow replacement retired; direct replacement documented | Keep UI/docs aligned |
| TF-STATUS-011 | High | closed | Rust Core schema fidelity | MySQL FK charset/collation fidelity | Keep FK fidelity regression coverage |
| TF-STATUS-012 | Medium | closed | Import UI telemetry | Cumulative Import rows/s and ETA | Re-check wording with real long-running imports |
| TF-STATUS-013 | High | closed | Rust Core import | MySQL JSON fallback encoding | Watch for malformed-source JSON reports |
| TF-STATUS-014 | Medium | closed | SQL editor UI | Large SQL rendering guard | Revisit virtual rendering if measured bottleneck remains |
| TF-STATUS-015 | Medium | closed | SQL editor UI | Schema/table tree panel | Consider richer query composition later |
| TF-STATUS-016 | Medium | closed | Rust Core dump.import diagnostics | MySQL ERROR 1114 table-full guidance | Collect target storage/tmpdir evidence if it recurs |
| TF-STATUS-017 | High | closed | Rust Core migration performance evidence | 1M/10M evidence archived and validated | Refresh if migration/verify streaming semantics change |
| TF-STATUS-018 | High | closed | Rust Core live migration / UI evidence | Bidirectional 1M live UI evidence captured | Refresh final validator evidence if migration/RSS semantics change |
| TF-STATUS-019 | Medium | closed | One-Click migration UI | Dry-run preview One-Click entry point | Keep preview evidence aligned if event payloads change |
| TF-STATUS-020 | High | closed | One-Click migration UI / Rust Core automatic fixes | Real execution and automatic fix coverage | Track any additional automatic fix class as a separate issue |
| TF-STATUS-021 | High | closed | One-Click migration UI / Rust Core automatic fixes | Charset/collation automatic fix coverage | Keep validator/live evidence aligned if the charset contract changes |
| TF-STATUS-022 | High | closed | One-Click migration UI / Rust Core automatic fixes | Derive charset contracts for PyQt execution | Keep derivation evidence aligned if PyQt payload construction or Rust Core derivation changes |
| TF-STATUS-023 | Medium | closed | One-Click migration UI / Rust Core automatic fixes | Align `int_display_width` skip semantics | Keep display-only skip policy aligned if Rust Core begins emitting this class |
| TF-STATUS-024 | High | closed | Export/Import UI | Direct DB Rust Core endpoint host | Keep direct connector host coverage when worker construction changes |
| TF-STATUS-025 | High | closed | macOS release validation | Artifact lookup uses current main after PR merge | Keep artifact lookup default aligned with final report SHA policy |
| TF-STATUS-026 | Medium | closed | Docs/UI feature flags | Schedule guide hidden-feature wording | Rewrite as public guide only when schedule feature is re-enabled with evidence |
| TF-STATUS-027 | Medium | closed | One-Click migration docs | Limited production scope wording | Keep docs aligned if the real-execution allowlist expands |
| TF-STATUS-028 | Low | closed | Status documentation | Full Python suite count refresh | Refresh count when new tests are added and full pytest is rerun |
| TF-STATUS-029 | Low | closed | Status documentation | Baseline verification heading | Replace preservation note after a full broad baseline sweep is rerun |
| TF-STATUS-030 | Low | closed | Status documentation / Rust Core boundary audit | Current main next-issue re-audit | Keep #116 as the only open issue unless new repo-side evidence appears |
| TF-STATUS-031 | Low | closed | Status documentation | Baseline count refresh after re-audit coverage | Refresh counts when new tests are added and rerun |
| TF-STATUS-032 | Low | closed | Status documentation | Focused verification duplicate rows | Keep focused verification command rows unique |
| TF-STATUS-033 | Low | closed | Status documentation | Current baseline duplicate rows | Keep current baseline command rows unique |
| TF-STATUS-034 | Low | closed | Export/Import UI | Rust Core Export/Import menu wording | Keep Export/Import labels aligned with Rust Core ownership |
| TF-STATUS-035 | Low | closed | One-Click migration UI | One-Click fallback dry-run tooltip | Keep feature-flag fallback copy aligned with current support matrix |
| TF-STATUS-036 | Low | closed | One-Click migration UI | One-Click module scope docstring | Keep source comments aligned with the One-Click support matrix |
| TF-STATUS-037 | Low | closed | Build documentation | BUILD installer version examples | Keep build examples version-neutral or synced |
| TF-STATUS-038 | High | closed | macOS release validation | macOS manual workflow head policy | Keep final report/artifact/manual workflow SHA policies aligned |
| TF-STATUS-039 | Low | closed | Status documentation / Rust Core boundary audit | Post-merge next-issue external re-audit | Keep #116 external unless confirmed repo-side evidence appears |
| TF-STATUS-040 | High | closed | Rust Core baseline / Migration Auto-Fix Wizard | Legacy Python Auto-Fix Wizard mutations | Keep the legacy wizard dry-run/manual SQL only unless a future Rust Core-owned command is added |
| TF-STATUS-041 | High | closed | Rust Core baseline / Migration Auto-Fix Wizard | Legacy Auto-Fix core mutation APIs | Keep core legacy Auto-Fix APIs fail-closed for `dry_run=False` unless Rust Core owns the workflow |
| TF-STATUS-042 | High | closed | Rust Core baseline / Migration Analyzer cleanup | Legacy MigrationAnalyzer cleanup mutations | Keep cleanup actual execution disabled unless Rust Core owns the workflow |
| TF-STATUS-043 | Medium | closed | Rust Core baseline / Migration Analyzer cleanup worker | Legacy CleanupWorker actual cleanup mode | Keep cleanup worker actual execution disabled unless Rust Core owns the workflow |
| TF-STATUS-044 | Medium | closed | Rust Core baseline / DB connector helper API | Legacy MySQLConnector execute_many mutation helper | Keep generic Python batch mutation helper disabled unless Rust Core owns the workflow |
| TF-STATUS-045 | Low | closed | Status documentation / macOS release validation | Post-#146 next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-046 | Medium | closed | Release versioning | Post-release version drift | Keep source/package/installer versions ahead of the latest released tag before release tagging |
| TF-STATUS-047 | Medium | closed | Release publication | v2.1.7 release publication | Keep release tags/assets aligned when version bumps land directly on main |
| TF-STATUS-048 | Low | closed | Status documentation / macOS release validation | Post-#148 next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-049 | Medium | closed | Release versioning | Post-v2.1.7 version drift | Keep source/package/installer versions ahead of the latest released tag before release tagging |
| TF-STATUS-050 | Medium | closed | Rust Core baseline / DB connector shim | RustDbCursor executemany batch helper | Keep generic Python batch helpers disabled unless Rust Core owns the batch operation |
| TF-STATUS-051 | Low | closed | Status documentation | Stale current pytest count wording | Keep current-tense full-suite wording aligned with the latest full `pytest -q` evidence |
| TF-STATUS-052 | Low | closed | Status documentation / macOS release validation | Post-#151 main merge and next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-053 | Low | closed | Status documentation | Post-#151 full-suite evidence refresh | Keep current full-suite count aligned when current-status tests are added |
| TF-STATUS-054 | Medium | closed | Rust Core query execution / SQL reporting | Rust Core DML affected row counts | Preserve Rust Core affected-row metadata in Python cursor shims |
| TF-STATUS-055 | Medium | closed | Rust Core Python shim / SQL reporting | Call-local affected-row metadata | Do not store per-query rowcount metadata on shared facade state |
| TF-STATUS-056 | High | closed | SQL execution / SQL Editor / Scheduler | SQL statement parser mismatch | Share one robust parser for SQL file execution, SQL Editor execute-all/current-query, and scheduled SQL |
| TF-STATUS-057 | Low | closed | SQL parser helper | SQL dollar quote helper guard | Keep dollar quote marker detection fail-closed for invalid start offsets |
| TF-STATUS-058 | Low | closed | Status documentation / macOS release validation | Post-#156 main merge and next issue analysis | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-059 | Low | closed | One-Click readiness docs | One-Click readiness next-action wording | Keep completed One-Click readiness guidance framed as standing policy, not current next repo-side work |
| TF-STATUS-060 | Low | closed | SQL parser helper | SQL dollar quote helper None input | Keep dollar quote marker detection fail-closed for invalid and missing SQL text |
| TF-STATUS-061 | Low | closed | Status documentation | Current-status baseline provenance refresh | Keep current baseline provenance tied to the latest status update |
| TF-STATUS-062 | Medium | closed | Rust Core baseline / Export | Partial export FK parent resolution | Keep partial Export FK parent auto-inclusion owned by Rust Core schema inspection, not Python DB connectors |
| TF-STATUS-063 | High | closed | Rust Core Export/Import | PostgreSQL Rust dump endpoint engine | Keep PostgreSQL Export/Import endpoint engine preserved through `RustDumpConfig` into Rust Core dump commands |
| TF-STATUS-064 | High | closed | Rust Core Export/Import | PostgreSQL Import timezone SQL | Keep PostgreSQL dump import from using MySQL timezone detection or MySQL timezone correction SQL |
| TF-STATUS-065 | High | closed | Rust Core dump.import | PostgreSQL Import timezone Core validation | Keep Rust Core timezone validation aligned with MySQL and PostgreSQL import timezone SQL forms |
| TF-STATUS-066 | Medium | closed | Rust Core Export/Import helper API | PostgreSQL dump wrapper engine | Keep module-level dump helper wrappers engine-aware while preserving MySQL default compatibility |
| TF-STATUS-067 | Medium | closed | Hidden Scheduler / Rust Core dump backup | Scheduled PostgreSQL backup engine | Keep scheduled backup `RustDumpConfig` engine derivation aligned with scheduled SQL connector derivation |
| TF-STATUS-068 | Medium | closed | Hidden Scheduler / Rust Core dump backup | Scheduled backup tuple connection info | Keep scheduled backup connection normalization aligned with real `TunnelEngine.get_connection_info()` tuple output |
| TF-STATUS-069 | Low | closed | Status documentation / macOS release validation | Post-#166 next issue re-audit | Keep #116 external until real operator Mac validation evidence is attached |
| TF-STATUS-070 | Low | closed | macOS release validation | Manual macOS workflow evidence refresh | Keep workflow_dispatch evidence refreshed before final real-Mac report finalization |
| TF-STATUS-071 | Low | closed | Status documentation / macOS release validation | Non-self-stale macOS workflow evidence policy | Keep exact current-head workflow run IDs/SHAs on #116 comments and final gate output, not as durable current-status summary evidence |
| TF-STATUS-072 | Low | closed | Status documentation / macOS release validation | Focused final-gate failure reason refresh | Keep current focused final-gate rows aligned with latest accepted current-head manual workflow evidence |
| TF-STATUS-073 | Low | closed | Status documentation / macOS release validation | Superseded missing manual workflow Summary cleanup | Keep Summary current-state paragraphs from presenting superseded missing manual workflow evidence as current |
| TF-STATUS-074 | Low | closed | Status documentation / repo-side re-audit | Post-#169 next issue re-audit | Keep #116 as the only open issue unless new repo-side evidence appears |
| TF-STATUS-075 | Low | closed | Status documentation / macOS release validation | macOS final validation tooling recheck | Keep #116 final validation tooling evidence fresh while external real-Mac report evidence remains pending |
| TF-STATUS-076 | Medium | closed | Security / Rust Core helper resolution | Frozen-runtime core helper lookup trusted boundary | Keep packaged runtime helper resolution limited to app-owned locations; allow PATH lookup only with explicit development opt-in |
| TF-STATUS-077 | Medium | closed | Security / auto-save paths | Schema-derived analysis and rollback filenames | Keep schema-derived filenames sanitized and resolved under their intended base directories |
| TF-STATUS-078 | Low | open | GitHub issue hygiene / Rust Core import | GitHub #170 remains open after merged ERROR 3780 fix | Confirm the PR #171 fix with the reporter and close #170 unless it reproduces on a containing release |
| TF-STATUS-079 | High | closed | Security / update integrity | Downloaded update package integrity verification | Keep GitHub Release asset `digest` verification fail-closed before every downloaded-package launch |
| TF-STATUS-080 | Medium | closed | Security / ProductionGuard | Unknown-environment dangerous-operation confirmation | Keep unknown-environment confirmation default-No for missing, unrecognized, and direct Import contexts |
| TF-STATUS-081 | High | fixed_pending_full_verify | Release readiness / versioning | `2.3.1` release candidate version alignment | Complete the external RC merge/tag process; do not create a tag or GitHub Release from this task |
| TF-STATUS-082 | Medium | closed | Product documentation / feature flags | Bilingual Schedule correction for disabled features | Keep both language surfaces explicit that Schedule remains disabled until intentional reactivation and verification |
| TF-STATUS-083 | Medium | fixed_pending_full_verify | CI / branch protection | Full Python regression workflow | Observe stable `python-regression` and Rust Core runs, then complete external required-check promotion |

## Recommended Execution Order

1. Keep TF-STATUS-079 closed by retaining GitHub Release asset `digest`
   verification before every downloaded-package launch.
2. Keep TF-STATUS-080 closed by retaining unknown-environment confirmation for
   dangerous operations without classified tunnel metadata.
3. Complete TF-STATUS-083 from `fixed_pending_full_verify` through external
   stable required-check promotion for `python-regression` and the Rust Core
   regression gate.
4. Keep TF-STATUS-082 closed by preserving the bilingual Schedule correction
   while the feature flag remains disabled.
5. Complete TF-STATUS-081 from `fixed_pending_full_verify` through the external
   RC merge/tag process for the `2.3.1` release candidate.
6. Complete TF-STATUS-008 / GitHub #116 on the frozen release candidate because
   #116 remains external, with both current-HEAD manual workflow evidence and
   the real-Mac report. Do not hard-code exact current-head workflow run IDs or
   SHAs as durable status summary evidence; use #116 comments and the final gate
   for current proof.
7. Resolve TF-STATUS-078: close #170 after confirming the merged fix from PR
   #171 / commit `a4c7a06`; reopen implementation work only if it reproduces on
   a release that contains the fix.
8. Defer another broad Clean Code round, Schedule reactivation, One-Click scope
   expansion, and Rust Core concurrency redesign until the release-trust work is
   complete and user/benchmark evidence justifies them.

## Session Log

| Date | Session Summary | Files Touched | Verification |
| --- | --- | --- | --- |
| 2026-07-10 | Addressed Task 6 review feedback by restoring the Round 3 historical `1827 passed / 6 warnings` assertion, separating current `2.3.1` RC and Rust evidence into a dedicated regression, preserving both records, and removing the duplicate Session Log delimiter. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED: 2 failed / 58 passed in 0.44s; GREEN: 60 passed in 0.25s; final diff check recorded with the fix commit |
| 2026-07-10 | Finalized the `2.3.1` release candidate status handoff: GitHub Release asset `digest` verification, unknown-environment confirmation, `python-regression`, and the bilingual Schedule correction are reflected in the tracker. TF-STATUS-079/080/082 are closed with fresh focused and full evidence; TF-STATUS-081/083 remain `fixed_pending_full_verify`; TF-STATUS-008/078 remain open. | `src/version.py`, `pyproject.toml`, `installer/TunnelForge.iss`, `docs/current_status.md`, `tests/test_current_status_docs.py` | RED: 1 failed / 57 passed in 0.43s; GREEN: 58 passed in 0.26s; full pytest: 1870 passed / 4 warnings in 60.08s; Rust gate exit 0 in 1.4s; Cargo test exit 0 in 4.1s; release build exit 0 in 36.61s; version sync 1 passed in 0.08s; diff check exit 0 in 0.5s |
| 2026-07-10 | Convened architecture, product, UX, quality, security, and critical-program-review agents for two rounds of repository-grounded strategy review. Consensus prioritizes update integrity, dangerous-SQL defaults, release truth, public capability accuracy, required regression gates, and real-Mac evidence before new features or broad refactors. | `docs/current_status.md`, `tests/test_current_status_docs.py` | six independent reviews plus cross-critique; direct source and GitHub verification; current-status pytest 56 passed; full pytest 1827 passed / 6 warnings; expected macOS final-gate failure for two missing evidence conditions |
| 2026-07-10 | Reconciled Round 3 completion against current Git and GitHub state. Round 3 remains complete and synchronized; #170 is open only because the already merged/released ERROR 3780 fix was not linked for automatic closure. | `docs/current_status.md`, `tests/test_current_status_docs.py` | `git` ancestry/sync checks; GitHub open-issue, #170, and PR #171 inspection; release-tag containment check; current-status pytest 55 passed; full pytest 1826 passed / 6 warnings |
| 2026-07-09 | Integrated Clean Code Round 3 WP-3.1 through WP-3.8 into `main`, covering SQL editor, DB dialogs, migration dialogs, Fix Wizard pages, cross-engine/diff dialogs, settings/schedule/tunnel dialogs, main window controllers, and UI workers. | Round 3 UI/core helper files plus `docs/current_status.md` | Round 3 focused pytest 491 passed; full `pytest -q` 1819 passed / 4 warnings; Rust Core regression gate passed; whole-tree `MySQLConnector` allowlist scan passed; `git diff --check` passed |
| 2026-07-09 | Addressed Clean Code Round 3 red-review findings by restoring migration worker legacy constructor compatibility, cleanup worker `dry_run=False` RuntimeError behavior, and Fix Wizard dialog re-export compatibility. | `src/ui/workers/migration_worker.py`, `src/ui/dialogs/fix_wizard_dialog.py`, `tests/test_migration_worker.py`, `tests/test_fix_wizard_dialog.py`, `docs/current_status.md` | RED/GREEN compatibility tests; focused migration/Fix Wizard suite 118 passed; full `pytest -q` 1821 passed / 4 warnings |
| 2026-07-09 | Addressed SECURE/APPROVE follow-up findings by restoring `BatchOptionDialog` legacy re-export, limiting core helper lookup trust boundaries, hardening schema-derived auto-save paths, and stabilizing Windows Git Bash packaging tests. | `src/core/cross_engine_migration.py`, `src/core/path_safety.py`, `src/ui/dialogs/migration_result_store.py`, `src/ui/dialogs/fix_wizard_execution_page.py`, `src/ui/dialogs/fix_wizard_dialog.py`, `scripts/check-macos-support-gate.py`, `scripts/macos-download-validation-artifacts.sh`, tests, `docs/current_status.md` | Security regression tests passed; focused review/security suites passed; macOS packaging pytest 51 passed; full `pytest -q` 1824 passed / 4 warnings; Rust Core regression gate passed; allowlist scan passed; `git diff --check` passed |
| 2026-06-27 | Recorded TF-STATUS-075 after rechecking #116 final validation tooling: shell syntax is valid, focused macOS support tests pass, the normal #116 gate passes, and the final gate fails only for missing external real-Mac report evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: final validation tooling current-status pytest; final: macOS focused pytest, #116 gates, shell syntax |
| 2026-06-27 | Recorded TF-STATUS-074 after a post-#169 next-issue re-audit found no new repo-side issue: #116 is still the only open GitHub issue, Rust Core boundary scans still route through shims, stale-handoff scans found no new current task, and the remaining blocker is external real-Mac report evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#169 current-status pytest; final: current-status pytest, full pytest, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-073 / GitHub #169 by removing superseded missing-manual-workflow current-state wording from the Summary; older verification log rows remain historical, while the Summary now keeps the #116 current blocker to real-Mac report evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #169 | RED/GREEN: superseded Summary wording current-status pytest; final: `pytest -q`, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-072 / GitHub #168 by refreshing the current focused final-gate row so it no longer lists missing current-head manual workflow evidence after that evidence was refreshed on #116; the current final-gate blocker is real-Mac report evidence only. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #168 | RED/GREEN: focused final-gate reason current-status pytest |
| 2026-06-27 | Fixed TF-STATUS-071 / GitHub #167 by changing current-status macOS workflow evidence handoff to avoid self-stale exact current-head run IDs/SHAs in durable status summary text; #116 comments and the final gate remain authoritative for the latest current-head workflow proof. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #167 | RED/GREEN: non-self-stale macOS workflow policy current-status pytest |
| 2026-06-27 | Triggered and verified manual `macOS App Validation` workflow_dispatch run `28264164795` for GitHub #116; both arm64 and x86_64 jobs passed for the then-current main HEAD, leaving only real-Mac manual validation report evidence before final closure. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116, GitHub Actions run 28264164795 | RED/GREEN: manual workflow current-status pytest; final gate expected-failing for missing real-Mac report only |
| 2026-06-27 | Re-audited the next issue after #166 and confirmed `main` was aligned with `origin/main`; #116 is the only open GitHub issue, the normal repo-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#166 current-status pytest; final gate expected-failing for external evidence only |
| 2026-06-27 | Fixed TF-STATUS-068 / GitHub #166 by normalizing real tuple-shaped scheduled backup connection info and resolving credentials before building `RustDumpConfig`. | `src/core/scheduler.py`, `tests/test_scheduler.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #166 | RED/GREEN: tuple connection backup pytest and current-status pytest; final: full `pytest -q` at 1869 passed |
| 2026-06-27 | Fixed TF-STATUS-067 / GitHub #165 by passing the normalized tunnel `db_engine` into scheduled backup `RustDumpConfig`, matching the existing scheduled SQL Rust Core connector path. | `src/core/scheduler.py`, `tests/test_scheduler.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #165 | RED/GREEN: scheduled backup engine pytest and current-status pytest; final: full `pytest -q` at 1867 passed |
| 2026-06-27 | Fixed TF-STATUS-066 / GitHub #164 by adding optional `engine` parameters to the module-level `export_schema`, `export_tables`, and `import_dump` convenience wrappers so PostgreSQL helper callers preserve Rust Core endpoint engines. | `src/exporters/rust_dump_exporter.py`, `tests/test_rust_dump_exporter.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #164 | RED/GREEN: wrapper engine pytest and current-status pytest; final: full `pytest -q` at 1865 passed |
| 2026-06-27 | Fixed TF-STATUS-065 / GitHub #163 by allowing Rust Core `dump.import` timezone validation to accept PostgreSQL `SET TIME ZONE` while preserving the existing MySQL `SET SESSION time_zone` allowlist and injection rejection. | `migration_core/src/lib.rs`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #163 | RED/GREEN: Rust timezone validator pytest and current-status pytest; final: Rust core tests, full `pytest -q` at 1861 passed |
| 2026-06-27 | Fixed TF-STATUS-064 / GitHub #162 by skipping MySQL timezone auto-detection for PostgreSQL dump import and using PostgreSQL `SET TIME ZONE` syntax for forced timezone options. | `src/ui/dialogs/db_dialogs.py`, `src/core/i18n.py`, `tests/test_db_dialogs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #162 | RED/GREEN: PostgreSQL import timezone pytest and current-status pytest; i18n regression; final: full `pytest -q` at 1860 passed |
| 2026-06-27 | Fixed TF-STATUS-063 / GitHub #161 by preserving PostgreSQL engine through RustDumpConfig, Export/Import dialog worker config, preselected PostgreSQL tunnel connectors, and Rust Core dump endpoints. | `src/exporters/rust_dump_exporter.py`, `src/ui/dialogs/db_dialogs.py`, `src/core/db_connector.py`, `src/core/postgres_connector.py`, `tests/test_rust_dump_exporter.py`, `tests/test_db_dialogs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #161 | RED/GREEN: PostgreSQL dump endpoint engine pytest, dialog worker config pytest, current-status pytest; full-suite count superseded by TF-STATUS-064 |
| 2026-06-27 | Fixed TF-STATUS-062 / GitHub #160 by routing partial Export FK parent resolution through Rust Core `schema.inspect` instead of Python `MySQLConnector`. | `src/exporters/rust_dump_exporter.py`, `tests/test_rust_dump_exporter.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #160 | RED/GREEN: partial export FK parent Rust inspect pytest and current-status pytest; exporter suite; full-suite count superseded by TF-STATUS-063 |
| 2026-06-27 | Fixed TF-STATUS-061 / GitHub #159 by refreshing the current-status baseline provenance wording after TF-STATUS-060. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #159 | RED/GREEN: current-status baseline provenance pytest; full-suite count superseded by TF-STATUS-062 |
| 2026-06-27 | Fixed TF-STATUS-060 / GitHub #158 by making the SQL dollar quote helper fail closed for `None` SQL text. | `src/core/sql_statement_parser.py`, `tests/test_sql_execution_worker.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #158 | RED/GREEN: dollar quote None-input pytest and current-status pytest; parser suite; final: full `pytest -q` at 1849 passed |
| 2026-06-27 | Fixed TF-STATUS-059 / GitHub #157 by changing One-Click readiness follow-up wording from a current next repo-side change to standing policy/watch guidance. | `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #157 | RED/GREEN: One-Click readiness docs pytest and current-status pytest; full-suite count superseded by TF-STATUS-060 |
| 2026-06-27 | Re-analyzed the next issue after #156 and confirmed `main` was already aligned with `origin/main`. #116 is the only open GitHub issue; the normal repository-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#156 current-status pytest; final: #116 gate pass, expected-failing final gate; full-suite count superseded by TF-STATUS-060 |
| 2026-06-27 | Fixed TF-STATUS-057 / GitHub #156 by making the SQL dollar quote helper fail closed for empty SQL text and out-of-range starts. | `src/core/sql_statement_parser.py`, `tests/test_sql_execution_worker.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #156 | RED/GREEN: dollar quote helper bounds pytest; parser suite, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-056 / GitHub #155 by extracting the robust SQL statement parser to `src/core/sql_statement_parser.py` and routing SQL file execution, SQL Editor split/current-query, and scheduled SQL through it. | `src/core/sql_statement_parser.py`, `src/ui/workers/test_worker.py`, `src/ui/dialogs/sql_editor_dialog.py`, `src/core/scheduler.py`, `tests/test_sql_editor_dialog.py`, `tests/test_scheduler.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #155 | RED/GREEN: SQL Editor/Scheduler parser tests; final: SQL Editor/Scheduler/worker pytest and full `pytest -q` |
| 2026-06-27 | Created TF-STATUS-056 / GitHub #155 after confirming that SQL Editor and hidden scheduler statement splitters can over-split comments, PostgreSQL dollar quote bodies, and MySQL `DELIMITER` scripts while SQL file execution already handles those cases. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #155 | RED/GREEN: SQL parser mismatch current-status pytest; #116 gate pass, expected-failing final gate |
| 2026-06-27 | Created and fixed TF-STATUS-055 / GitHub #154 after finding that the #153 Python cursor shim used shared facade state for affected-row metadata. | `src/core/db_core_service.py`, `tests/test_db_core_service.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #154 | RED/GREEN: call-local rowcount metadata pytest |
| 2026-06-27 | Created and fixed TF-STATUS-054 / GitHub #153 after finding that Rust Core DML execution returned empty rows without affected-row metadata, causing Python cursor shims to report `rowcount=0` for successful DML. | `migration_core/src/lib.rs`, `src/core/db_core_service.py`, `tests/test_db_core_service.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #153 | RED/GREEN: Rust/Python affected-row tests; focused scheduler/SQL worker tests |
| 2026-06-27 | Created and fixed TF-STATUS-053 / GitHub #152 after the post-#151 status coverage increased the full Python suite; the count is now superseded by TF-STATUS-054 at `1837 passed, 5 warnings`. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #152 | RED/GREEN: full-suite count current-status pytest; final: `pytest -q` |
| 2026-06-27 | Re-analyzed the next issue after #151 and confirmed `main` was aligned with `origin/main` before this status update, then pushed this status update to `origin/main`. #116 is the only open GitHub issue; the normal repository-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#151 current-status pytest; final: #116 gate pass, expected-failing final gate |
| 2026-06-27 | Created and fixed TF-STATUS-051 / GitHub #151 after finding stale current-tense `1830 passed` wording left behind after the #150 full-suite run. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #151 | RED/GREEN: stale current-count current-status pytest |
| 2026-06-27 | Created and fixed TF-STATUS-050 / GitHub #150 after finding the unused `RustDbCursor.executemany` Python-side batch helper. | `src/core/db_core_service.py`, `tests/test_db_core_service.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #150 | RED/GREEN: RustDbCursor executemany pytest and current-status pytest; full pytest count superseded by TF-STATUS-053 |
| 2026-06-27 | Created and fixed TF-STATUS-049 / GitHub #149 after finding that `main` had post-`v2.1.7` commits while source/package/installer references still declared `2.1.7`. | `src/version.py`, `pyproject.toml`, `installer/TunnelForge.iss`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #149 | RED/GREEN: post-v2.1.7 version drift current-status pytest; version sync pytest; full pytest count superseded by TF-STATUS-050/051 |
| 2026-06-27 | Re-analyzed the next issue after #148 closure. #116 is the only open GitHub issue; the normal repository-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#148 current-status pytest; final: #116 gate pass, expected-failing final gate |
| 2026-06-27 | Created and fixed TF-STATUS-047 / GitHub #148 after direct `main` version bumping left release publication behind; pushed tag `v2.1.7`, verified Build and Release workflow run `28255274238`, and confirmed the GitHub release assets. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #148, release `v2.1.7` | RED/GREEN: v2.1.7 release-publication current-status pytest; final: release workflow success and `gh release view v2.1.7` |
| 2026-06-27 | Created and fixed TF-STATUS-046 / GitHub #147 after finding that `main` still declared `2.1.6` even though release/tag `v2.1.6` already exists and post-release commits have accumulated. | `src/version.py`, `pyproject.toml`, `installer/TunnelForge.iss`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #147 | RED/GREEN: post-release version drift current-status pytest; version sync pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
| 2026-06-27 | Re-analyzed the next issue after #146. #116 is still the only open GitHub issue; the normal repo-side macOS support gate passes, and the final gate remains blocked only by missing current-main real-Mac evidence and manual workflow_dispatch evidence. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#146 current-status pytest; final: #116 gate pass, expected-failing final gate, current-status pytest |
| 2026-06-27 | Created and fixed TF-STATUS-044 / GitHub #146 after finding the unused `MySQLConnector.execute_many` public helper still exposed a Python-owned batch mutation/commit API. | `src/core/db_connector.py`, `tests/test_db_connector.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #146 | RED/GREEN: connector helper pytest and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
| 2026-06-27 | Created and fixed TF-STATUS-043 / GitHub #145 after finding that `CleanupWorker(..., dry_run=False)` still accepted legacy actual cleanup mode after #144 fail-closed the analyzer and dialog paths. | `src/ui/workers/migration_worker.py`, `tests/test_migration_worker.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #145 | RED/GREEN: cleanup worker pytest and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
| 2026-06-27 | Created and fixed TF-STATUS-042 / GitHub #144 after finding that `MigrationAnalyzer.execute_cleanup(..., dry_run=False)` still provided a Python-owned cleanup mutation path and the dialog could expose actual cleanup execution. | `src/core/migration_analyzer.py`, `src/ui/dialogs/migration_dialogs.py`, `src/core/i18n.py`, `tests/test_migration_analyzer.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #144 | RED/GREEN: cleanup mutation pytest, migration analyzer dialog pytest, i18n pytest, and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
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
| 2026-06-26 | Hardened MySQL JSON fallback INSERT handling for GitHub #118 by using `_utf8mb4` JSON literals and removing `NO_BACKSLASH_ESCAPES` during import session tuning. | `migration_core/src/lib.rs`, `docs/current_status.md`, `reports/export_import_flow_review_20260601.html` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml mysql_json_literal_uses_utf8mb4_introducer_for_unicode_json_text --lib`; `cargo test --manifest-path migration_core\Cargo.toml mysql_dump_import_uses_fast_session_tuning_statements --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `pytest -q`; `python -m compileall -q main.py src tests`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added a large-document guard for GitHub #86 so SQL files at or above 512KB open with syntax highlighting and real-time validation disabled, then restore normal editor features for smaller content. | `src/ui/dialogs/sql_editor_dialog.py`, `src/core/i18n.py`, `tests/test_sql_editor_dialog.py`, `docs/current_status.md` | RED/GREEN: `pytest tests/test_sql_editor_dialog.py::test_large_sql_file_disables_expensive_editor_features tests/test_sql_editor_dialog.py::test_small_content_reenables_editor_features_after_large_file`; final: `pytest tests/test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests/test_sql_editor_dialog.py`; `pytest -q`; `python -m compileall -q main.py src tests`; `git diff --check` |
| 2026-06-26 | Added the SQL editor schema/table tree panel for GitHub #92 with schema roots, loaded table/column children, and table-click insertion into the current editor. | `src/ui/dialogs/sql_editor_dialog.py`, `tests/test_sql_editor_dialog.py`, `docs/current_status.md` | RED/GREEN: `pytest tests/test_sql_editor_dialog.py::test_metadata_loaded_populates_schema_tree tests/test_sql_editor_dialog.py::test_schema_tree_table_click_inserts_quoted_table_name`; final: `pytest tests/test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation tests/test_sql_editor_dialog.py`; `pytest -q`; `python -m compileall -q main.py src tests`; `git diff --check` |
| 2026-06-26 | Analyzed GitHub #126 and added MySQL `ERROR 1114` storage/tmpdir guidance to post-load DDL import failures. | `migration_core/src/lib.rs`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_mysql_table_full_error_includes_storage_guidance --lib`; focused regression: `cargo test --manifest-path migration_core\Cargo.toml post_load_ddl --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Re-audited GitHub #116 after #126 closure: PR #117 is merged and local codebase gates pass, but #116 remains open only for final real operator Mac evidence. | `docs/current_status.md` | `gh pr view 117 --repo sanghyun-io/tunnelforge`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` |
| 2026-06-26 | Analyzed GitHub #99 and created #135 for the remaining Rust Core 1M/10M performance evidence durability gap. | `docs/current_status.md` | `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `git status --ignored --short migration_core\target\perf_*.jsonl`; `gh issue create` |
| 2026-06-26 | Archived Rust Core 1M/10M performance evidence under `reports\rust_core_performance`, added a validator, and wired the optional performance regression gate to the archived evidence for GitHub #135/#99. | `reports/rust_core_performance`, `scripts/validate-rust-core-performance-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_rust_core_performance_evidence.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_performance_evidence.py -q`; final: `python scripts\validate-rust-core-performance-evidence.py`; `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Audited GitHub #99 closure criteria after #135 and created #136 for the remaining live bidirectional 1M UI responsiveness evidence. | `migration_core/src/lib.rs`, `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml`; focused Python Rust Core/UI plumbing tests; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `RUST_CORE_REQUIRE_PERF_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `rg` direct DB driver scan |
| 2026-06-26 | Added a machine-checkable #136 live UI migration evidence validator and JSON template so future real 1M bidirectional runs can be accepted or rejected consistently. | `scripts/validate-live-ui-migration-evidence.py`, `tests/test_live_ui_migration_evidence.py`, `reports/live_ui_migration`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_live_ui_migration_evidence.py -q`; final: `python -m compileall -q scripts tests`; `git diff --check` |
| 2026-06-26 | Analyzed GitHub #136 after merging prior work to main; confirmed local MySQL/PostgreSQL live endpoint wiring passes small Rust Core roundtrip tests, but #136 still requires durable 1M bidirectional PyQt heartbeat evidence. | `docs/current_status.md` | `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip -- --nocapture` |
| 2026-06-26 | Added the #136 live UI evidence capture helper with deterministic local-container seeding, CrossEngineMigrationWorker execution, Qt heartbeat sampling, and validator-compatible report generation; verified the path with a 1,000-row smoke that must not be used as final evidence. | `scripts/capture-live-ui-migration-evidence.py`, `tests/test_live_ui_migration_capture.py`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_live_ui_migration_capture.py -q`; smoke: `python scripts\capture-live-ui-migration-evidence.py --rows 1000 ...`; expected reject: `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-smoke.json` |
| 2026-06-26 | Captured and preserved partial #136 evidence for the live 1M bidirectional PyQt worker path; both directions passed migrate+verify with heartbeat max gap 125ms, leaving only real 10M RSS evidence before final validator closure. | `reports/live_ui_migration/live-ui-migration-evidence-1m-local-partial.json`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | `python scripts\capture-live-ui-migration-evidence.py --rows 1000000 ...`; expected reject: `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence-1m-local-partial.json` |
| 2026-06-26 | Added and ran the Rust Core 10M synthetic stress RSS harness, generated the final #136 evidence file, and closed TF-STATUS-018 after the final validator passed. | `migration_core/tests/stress_rss.rs`, `reports/live_ui_migration/stress-10m-rss.json`, `reports/live_ui_migration/live-ui-migration-evidence.json`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml --test stress_rss synthetic_stress_run_reports_resume_verify_and_rss_bound -- --nocapture`; ignored 10M: `cargo test --manifest-path migration_core\Cargo.toml --test stress_rss synthetic_10m_stress_resume_verify_reports_rss_bound -- --ignored --nocapture`; final: `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json` |
| 2026-06-26 | Re-audited the last open issue #116 after #99/#136 closure; local macOS support gates still pass, but the issue remains open for external real operator Mac evidence. | `docs/current_status.md` | `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q scripts tests` |
| 2026-06-26 | Cleaned up stale wording after closing #99/#136 so the evidence READMEs and status heading describe completed evidence instead of pending closure gates. | `docs/current_status.md`, `reports/live_ui_migration/README.md`, `reports/rust_core_performance/README.md` | `rg -n "remaining #99|GitHub issue #136 now tracks|Live UI Performance Evidence Pending|should remain open until the live|#99 remains open|#136 still remains open" docs reports scripts tests README.md README.ko.md`; `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json`; `python scripts\validate-rust-core-performance-evidence.py`; `git diff --check` |
| 2026-06-26 | Wired final live UI evidence into the optional Rust Core regression gate so clean checkouts can require both archived Rust performance evidence and live UI evidence. | `scripts/rust-core-regression-gate.ps1`, `tests/test_live_ui_migration_evidence.py`, `reports/live_ui_migration/README.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_live_ui_migration_evidence.py::test_regression_gate_can_require_live_ui_evidence -q`; `RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Reconfirmed current `main` was aligned with `origin/main`, ran a broader current-main verification sweep, and re-analyzed GitHub #116 as the then-only remaining open issue before the later One-Click tracker was created. | `docs/current_status.md` | `pytest -q`; `cargo test --manifest-path migration_core\Cargo.toml`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python -m compileall -q main.py src tests scripts`; `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json`; `python scripts\validate-rust-core-performance-evidence.py`; `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `git diff --check` |
| 2026-06-26 | Audited stale plan/TODO candidates after #116 was confirmed external; found the One-Click Rust Core command surface exists while the PyQt entry point remains hidden, created GitHub #137, and added TF-STATUS-019 so the production-readiness gate is tracked separately from closed #124. | `docs/current_status.md` | `rg -n "oneclick\.|ONE_CLICK_MIGRATION_FEATURE_ENABLED" migration_core\src\lib.rs src tests docs README.md README.ko.md`; `tunnelforge-core service.hello`; `gh issue view 124`; `gh issue create` created #137 |
| 2026-06-26 | Hardened the hidden One-Click path for #137 so real execution is blocked until the readiness gate opens and the hidden dialog cannot uncheck Dry-run. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_rejects_real_execution_until_readiness_gate_opens -q`; RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_locks_dry_run_until_readiness_gate_opens -q`; final: `pytest tests\test_oneclick_rust_core_gate.py tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `pytest tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py`; `git diff --check` |
| 2026-06-26 | Added One-Click dry-run evidence capture/validation tooling, archived local MySQL Rust Core `oneclick.run` dry-run evidence, documented the current hidden dry-run-only scope, and wired the optional regression gate to that evidence. | `scripts/validate-oneclick-dry-run-evidence.py`, `scripts/capture-oneclick-dry-run-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_dry_run_evidence.py`, `reports/oneclick_readiness`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_dry_run_evidence.py -q`; capture: `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; final: `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE=1 powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Analyzed the next #137 decision after merging One-Click evidence: current Rust Core behavior supports hidden or dry-run-only preview scope, but not full enablement because automatic fix coverage is not implemented. | `docs/oneclick_readiness.md`, `docs/current_status.md` | `rg -n "ONE_CLICK_MIGRATION_FEATURE_ENABLED|ONECLICK_REAL_EXECUTION_ENABLED|oneclick|OneClick" src migration_core tests docs README.md README.ko.md`; `gh issue view 137`; Rust Core `oneclick_*` function inspection |
| 2026-06-26 | Exposed #137 as a dry-run-only preview: the migration analyzer shows `One-Click Dry-run Preview`, real execution remains blocked, and refreshed evidence now requires preview UI enabled plus real execution disabled. | `src/ui/dialogs/migration_dialogs.py`, `src/core/i18n.py`, `scripts/validate-oneclick-dry-run-evidence.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_oneclick_dry_run_evidence.py`, `reports/oneclick_readiness`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_migration_analyzer_exposes_oneclick_as_dry_run_preview_only -q`; RED/GREEN: `pytest tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_accepts_complete_report tests\test_oneclick_dry_run_evidence.py::test_oneclick_dry_run_evidence_requires_preview_ui_enabled -q`; i18n: `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; capture: `python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json`; validator: `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json` |
| 2026-06-26 | Split the remaining One-Click real-execution work into GitHub #138, marked TF-STATUS-019 as the closed dry-run preview gate, opened TF-STATUS-020 for automatic fix coverage, and updated the real-execution lock copy to point at #138. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/current_status.md`, `docs/oneclick_readiness.md` | `gh issue create` created #138; `gh issue view 137`; `gh issue view 138`; RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_rejects_real_execution_until_readiness_gate_opens tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_locks_dry_run_until_readiness_gate_opens -q`; `rg -n "TF-STATUS-019|TF-STATUS-020|#138|ONECLICK_REAL_EXECUTION_ENABLED" docs src tests migration_core` |
| 2026-06-26 | Started GitHub #138 automatic-fix coverage by adding typed Rust Core recommendation metadata: `deprecated_engine` with `table_name` becomes an `engine_innodb` automatic candidate while real execution remains disabled. | `migration_core/src/lib.rs`, `tests/test_oneclick_rust_core_gate.py`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_recommend_classifies_deprecated_engine_as_auto_fixable --lib` |
| 2026-06-26 | Added the #138 real-execution evidence validator and optional regression-gate hook without enabling real execution. The validator requires controlled local MySQL evidence for `deprecated_engine -> engine_innodb`, safe `tf_oneclick_` schema scope, app real execution still disabled, no disallowed fix attempts, and before/after `InnoDB` proof. | `scripts/validate-oneclick-real-execution-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_real_execution_evidence.py`, `reports/oneclick_readiness/oneclick-real-execution-evidence.template.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_real_execution_evidence.py -q`; final: `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; template expected reject: `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json`; `python -m compileall -q scripts tests`; `git diff --check` |
| 2026-06-26 | Added the Rust Core `oneclick.apply_fixes` execution path for the first allowed automatic fix only: `deprecated_engine -> engine_innodb`. The command now plans allowed actions, skips manual/skip steps, blocks disallowed strategies, requires a MySQL endpoint for real execution, and executes through Rust `MigrationAdapter::execute_sql`; PyQt real execution remains disabled and local before/after evidence is still pending. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_actions_accepts_only_engine_innodb_steps --lib`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_plan_executes_engine_innodb_sql --lib`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_fixes_real_engine_innodb_requires_endpoint --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml`; `pytest tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py -q`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; template expected reject: `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.template.json`; `git diff --check` |
| 2026-06-26 | Added and ran the controlled local real-execution evidence capture for #138. The archived evidence proves Rust Core `oneclick.apply_fixes` changed the test table from `MyISAM` to `InnoDB`; the PyQt real-execution flag remains disabled because `oneclick.run` still needs UI-facing automatic-fix orchestration. | `src/core/db_core_service.py`, `scripts/capture-oneclick-real-execution-evidence.py`, `tests/test_db_core_service.py`, `tests/test_oneclick_real_execution_capture.py`, `reports/oneclick_readiness/oneclick-real-execution-evidence.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_db_core_service.py::test_facade_uses_oneclick_apply_fixes_protocol -q`; RED/GREEN: `pytest tests\test_oneclick_real_execution_capture.py -q`; capture: `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-real-execution-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-real-execution-evidence.json`; final: `pytest tests\test_oneclick_real_execution_capture.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_rust_core_gate.py tests\test_db_core_service.py -q`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q src\core\db_core_service.py scripts\capture-oneclick-real-execution-evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py`; `git diff --check` |
| 2026-06-26 | Added live MySQL discovery for deprecated engine One-Click candidates. Rust Core now marks MyISAM base tables during inspection and converts those markers into typed `deprecated_engine` issues for `engine_innodb` recommendations. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick_issues_classify_deprecated_engine_marker_as_auto_fixable --lib`; RED/GREEN: `cargo test --manifest-path migration_core\Cargo.toml mysql_deprecated_engine_sql_targets_table_engines --lib`; final: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo test --manifest-path migration_core\Cargo.toml`; `python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Connected `oneclick.run dry_run=false` to the validated `engine_innodb` apply path. The live MySQL regression creates a MyISAM table, runs the UI-facing Rust command, and verifies the table becomes InnoDB. | `migration_core/src/lib.rs`, `migration_core/tests/live_roundtrip.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture` |
| 2026-06-26 | Opened the PyQt One-Click real-execution gate only for the validated `deprecated_engine -> engine_innodb` scope, kept Dry-run as the default, required backup confirmation for non-dry-run payloads, updated evidence validators/docs/status, and prepared GitHub #138 for closure. | `src/ui/dialogs/oneclick_migration_dialog.py`, `src/ui/dialogs/migration_dialogs.py`, `src/core/i18n.py`, `scripts/validate-oneclick-dry-run-evidence.py`, `scripts/validate-oneclick-real-execution-evidence.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_oneclick_dry_run_evidence.py`, `tests/test_oneclick_real_execution_evidence.py`, `docs/oneclick_readiness.md`, `docs/current_status.md`, `reports/oneclick_readiness/README.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py -q`; final: `pytest tests\test_oneclick_rust_core_gate.py tests\test_oneclick_dry_run_evidence.py tests\test_oneclick_real_execution_evidence.py tests\test_oneclick_real_execution_capture.py tests\test_db_core_service.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; live: `cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_engine_innodb_when_env_is_configured --test live_roundtrip -- --nocapture`; evidence gate: `$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'; $env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `python -m compileall -q ...`; `git diff --check` |
| 2026-06-26 | Closed GitHub #138, scanned remaining open issues, and re-audited #116 as the only remaining open issue. The macOS support gate and focused tests still pass; #116 remains blocked only on real operator Mac evidence. | `docs/current_status.md` | `gh issue list --repo sanghyun-io/tunnelforge --state open --limit 20 --json number,title,labels,url`; `gh issue view 116 --repo sanghyun-io/tunnelforge --json number,title,state,body,comments,url,labels`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` |
| 2026-06-26 | Created GitHub #139 and TF-STATUS-021 for the next actionable One-Click automatic-fix class: charset/collation coverage. | `docs/current_status.md`, `docs/oneclick_readiness.md` | `rg -n "charset_issue|invalid_date|zerofill_usage|float_precision|enum_empty_value|deprecated_engine|engine_innodb|manual|oneclick_recommend|oneclick_apply" migration_core\src\lib.rs tests docs\oneclick_readiness.md`; `gh issue create` created #139 |
| 2026-06-26 | Added the #139 charset/collation evidence validator, JSON template, and optional regression-gate hook without enabling charset real execution. | `scripts/validate-oneclick-charset-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_charset_evidence.py`, `reports/oneclick_readiness/oneclick-charset-evidence.template.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_charset_evidence.py -q`; expected reject: `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.template.json`; expected reject until evidence capture: `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Documented the #139 charset/collation automation policy boundary before enabling any Rust Core recommendation or execution path. | `docs/oneclick_readiness.md`, `docs/current_status.md` | Policy-only change; no charset real execution enabled |
| 2026-06-26 | Historical row: reconfirmed the latest changes were already on `main`/`origin/main` and analyzed the next open issue at that time. #139 was then the next in-repo issue; #116 remained external real-Mac evidence. The next safe #139 step was evidence capture/report scaffolding before any Rust Core charset allowlist expansion. #139 is now closed. | `docs/current_status.md` | `git status --short --branch`; `git log --oneline --decorate -8`; `gh issue list --state open --limit 30 --json number,title,labels,updatedAt,createdAt,url,assignees`; `gh issue view 139 --comments --json ...`; `gh issue view 116 --json ...`; `rg -n "charset_issue|charset|collation|oneclick_auto_fix_option|oneclick_apply_actions|engine_innodb|deprecated_engine" migration_core\src\lib.rs tests docs\oneclick_readiness.md src` |
| 2026-06-26 | Added the #139 charset/collation capture/report scaffold without enabling live charset execution. The report builder produces validator-backed evidence shape from captured inputs, unsafe `tf_oneclick_` scope checks run before capture, and the live capture entry point fails closed until Rust Core implements the allowlisted path. | `scripts/capture-oneclick-charset-evidence.py`, `tests/test_oneclick_charset_capture.py`, `docs/oneclick_readiness.md`, `reports/oneclick_readiness/README.md`, `docs/current_status.md` | RED: `pytest tests\test_oneclick_charset_capture.py -q` failed because `scripts\capture-oneclick-charset-evidence.py` did not exist; RED: `pytest tests\test_oneclick_charset_capture.py::test_oneclick_charset_capture_cli_fails_closed_without_traceback -q` failed because the CLI raised `CaptureNotImplementedError`; GREEN: `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; expected fail-closed: `python scripts\capture-oneclick-charset-evidence.py --schema tf_oneclick_charset`; expected template reject: `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.template.json`; `python -m compileall -q scripts\capture-oneclick-charset-evidence.py tests\test_oneclick_charset_capture.py`; `git diff --check` |
| 2026-06-26 | Added an internal Rust Core #139 contract helper for future `charset_issue -> charset_collation_fk_safe` options without wiring it into recommendation or execution paths. The helper validates safe evidence identifiers, explicit target charset/collation, FK-order coverage, rollback SQL, and generated table-level conversion SQL. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_charset_contract --lib` failed because `oneclick_charset_fk_safe_option_from_payload` did not exist; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Connected the #139 Rust Core charset contract to recommendation and dry-run preview only. Complete `charset_contracts[]` data can produce a `charset_collation_fk_safe` recommendation and `oneclick.apply_fixes dry_run=true` `planned_fixes`; missing contract data remains manual. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_recommend_gates_charset_auto_fix_on_complete_contract --lib` failed with `auto_fixable` 0; RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_fixes_dry_run_previews_charset_plan_without_execution_allowlist --lib` failed with disallowed charset dry-run; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Added command-level Rust Core charset execution planning for complete `charset_collation_fk_safe` contracts. The adapter path executes generated charset SQL in FK order, preserves rollback SQL/target/fk_order metadata in applied fixes, and reports SQL failure with rollback metadata. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_apply_plan_executes_charset_sql_in_fk_order_with_rollback_metadata --lib` failed because no charset SQL executed; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `git diff --check` |
| 2026-06-26 | Implemented and captured #139 local MySQL charset/collation evidence through Rust DB Core. The completed report proves `oneclick.apply_fixes dry_run=false` changed the local FK-connected `tf_oneclick_charset` tables from `utf8mb3`/`utf8mb3_general_ci` to `utf8mb4`/`utf8mb4_0900_ai_ci`, preserved FK evidence, and includes rollback metadata. | `scripts/capture-oneclick-charset-evidence.py`, `tests/test_oneclick_charset_capture.py`, `reports/oneclick_readiness/oneclick-charset-evidence.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `pytest tests\test_oneclick_charset_capture.py::test_oneclick_charset_capture_orchestrates_validator_backed_live_report -q` failed because `capture_oneclick_charset` did not accept a facade; GREEN: `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-charset-evidence.py --seed-local-container --mysql-container tf-live-mysql --mysql-host 127.0.0.1 --mysql-port 3406 --mysql-user root --mysql-password test --schema tf_oneclick_charset --output reports\oneclick_readiness\oneclick-charset-evidence.json`; `python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` |
| 2026-06-26 | Connected UI-facing Rust Core `oneclick.run dry_run=false` to supplied complete #139 charset contracts. The command now merges payload issues with inspection-derived issues, shifts charset contract indexes safely, and executes the same allowlisted `charset_collation_fk_safe` apply path. | `migration_core/src/lib.rs`, `migration_core/tests/live_roundtrip.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_charset_contract_when_env_is_configured --test live_roundtrip -- --nocapture` |
| 2026-06-26 | Added PyQt coverage for #139 charset execution-plan rendering/count copy, split automatic PyQt charset contract derivation into GitHub #140 / TF-STATUS-022, and closed TF-STATUS-021 after final gates passed. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_dialog_renders_charset_plan_counts_and_copy -q`; `pytest tests\test_oneclick_rust_core_gate.py -q`; `pytest tests\test_i18n.py::test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation -q`; `python -m compileall -q src\ui\dialogs\oneclick_migration_dialog.py tests\test_oneclick_rust_core_gate.py`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; live: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_run_live_charset_contract_when_env_is_configured --test live_roundtrip -- --nocapture`; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py -q`; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check`; `gh issue create` created #140 |
| 2026-06-26 | Started #140 by adding a Rust Core `oneclick.derive_charset_contracts` command and pure facts-based derivation helper. The helper derives complete local-safe `charset_contracts[]` only from safe table facts, FK closure/order, explicit target charset/collation, and rollback SQL; unsafe or incomplete facts produce no contract. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_derives_charset_contract --lib` failed because derivation structs/helper did not exist; RED: `cargo test --manifest-path migration_core\Cargo.toml oneclick_derive_charset_contracts_command_returns_contracts_from_safe_facts --lib` failed because no result command existed; GREEN: `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo test --manifest-path migration_core\Cargo.toml service_hello_advertises_core_protocol --lib` |
| 2026-06-26 | Extended #140 derivation from static facts to live Rust-owned MySQL facts and connected PyQt payload construction to `oneclick.derive_charset_contracts`. The Rust command now synthesizes safe charset issues/contracts from live `information_schema` facts, and `OneClickMigrationWorker._core_payload()` includes derived issues/contracts only when the derivation gate returns both. | `migration_core/src/lib.rs`, `migration_core/tests/live_roundtrip.rs`, `src/core/db_core_service.py`, `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_db_core_service.py`, `tests/test_oneclick_rust_core_gate.py`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `TF_MYSQL_HOST=127.0.0.1; TF_MYSQL_PORT=3406; TF_MYSQL_USER=root; TF_MYSQL_PASSWORD=test; TF_MYSQL_DATABASE=tf_oneclick_real_execution; cargo test --manifest-path migration_core\Cargo.toml oneclick_derive_charset_contracts_live_facts_when_env_is_configured --test live_roundtrip -- --nocapture`; RED/GREEN: `pytest tests\test_db_core_service.py::test_facade_uses_oneclick_derive_charset_contracts_protocol -q`; RED/GREEN: `pytest tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_includes_derived_charset_contracts_when_gate_passes tests\test_oneclick_rust_core_gate.py::test_oneclick_worker_omits_charset_contracts_when_derivation_gate_fails -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `python -m compileall -q src\core\db_core_service.py src\ui\dialogs\oneclick_migration_dialog.py tests\test_db_core_service.py tests\test_oneclick_rust_core_gate.py` |
| 2026-06-26 | Rechecked `main`/`origin/main` after the merge request and analyzed the next open issue. The #140 commits are already on `main`; #140 should continue with derivation-specific validator-backed local evidence, and #116 remains separate because it needs real operator Mac validation. | `docs/current_status.md` | `git fetch origin --prune`; `git status --short --branch`; `gh issue list --state open --limit 20 --json number,title,labels,updatedAt,assignees,url`; `gh issue view 140 --comments --json ...`; `gh issue view 116 --json ...`; `rg -n "TF-STATUS-022|#140|derive_charset|oneclick.derive_charset|charset_contracts|OneClickMigrationWorker|derive_oneclick_charset_contracts" ...` |
| 2026-06-26 | Added validator-backed #140 local evidence for PyQt-triggered charset derivation, closed TF-STATUS-022, and closed GitHub #140. The archived report proves `OneClickMigrationWorker._core_payload()` calls Rust Core derivation, includes derived `issues[]` / `charset_contracts[]`, and `oneclick.run dry_run=false` converts the FK-connected local tables. | `scripts/validate-oneclick-charset-derivation-evidence.py`, `scripts/capture-oneclick-charset-derivation-evidence.py`, `scripts/rust-core-regression-gate.ps1`, `tests/test_oneclick_charset_derivation_evidence.py`, `tests/test_oneclick_charset_derivation_capture.py`, `reports/oneclick_readiness/oneclick-charset-derivation-evidence.json`, `reports/oneclick_readiness/README.md`, `docs/oneclick_readiness.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_charset_derivation_evidence.py -q`; RED/GREEN: `pytest tests\test_oneclick_charset_derivation_capture.py -q`; final: `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py tests\test_oneclick_charset_derivation_capture.py tests\test_oneclick_charset_derivation_evidence.py -q`; `cargo build --manifest-path migration_core\Cargo.toml --release`; capture: `python scripts\capture-oneclick-charset-derivation-evidence.py --seed-local-container ...`; validator: `python scripts\validate-oneclick-charset-derivation-evidence.py reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; gate: `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_DERIVATION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1`; `gh issue comment 140`; `gh issue close 140` |
| 2026-06-26 | Created GitHub #141 and TF-STATUS-023 for the next One-Click repo-side follow-up: resolving the contradictory `int_display_width` skip/manual policy before any implementation. | `docs/current_status.md` | `rg -n "invalid_date|zerofill_usage|float_precision|int_display_width|enum_empty_value|manual|skip|oneclick_issues_from_inspection|oneclick_recommendations|oneclick_auto_fix_option" ...`; `gh issue create` created #141 |
| 2026-06-26 | Resolved #141 / TF-STATUS-023 by documenting `int_display_width` as display-only skip: PyQt may render externally supplied skip payloads, but Rust Core live One-Click does not synthesize this class and `skip` never executes SQL. | `migration_core/src/lib.rs`, `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; final: `pytest tests\test_oneclick_readiness_docs.py tests\test_oneclick_rust_core_gate.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick_live_inspection_does_not_synthesize_int_display_width_skip --lib`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` |
| 2026-06-26 | Refreshed the current status Summary after #139-#141 closure so new sessions do not treat a closed One-Click issue as the next repo-side task; re-analyzed #116 as the only open issue. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final: `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py -q`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `git diff --check`; `gh issue list --state open --limit 30 --json number,title,labels,updatedAt,url` |
| 2026-06-26 | Scanned current code/docs for untracked TODO, disabled-feature, and stale next-issue wording; found no new repo-side issue beyond external #116 and corrected one stale top Verification Log note about now-closed #140. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final: `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py -q`; `git diff --check` |
| 2026-06-26 | Fixed the #116 macOS support gate for the current merged-PR state. The gate now treats PR #117 `state=MERGED` as satisfying merge-state readiness even when GitHub reports `mergeStateStatus=UNKNOWN`, while keeping status-check validation active. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state -q`; final: `python scripts\check-macos-support-gate.py`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q scripts\check-macos-support-gate.py tests\test_rust_core_packaging.py`; `git diff --check` |
| 2026-06-26 | Updated the #116 final gate SHA comparison for merged PR reality: before merge, final reports still match PR #117 head; after merge, they match current merged main HEAD so operators can finalize from the current repository state. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/macos_support.md`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_final_report_after_pr_merge -q`; RED/GREEN: `pytest tests\test_macos_support_docs.py -q`; final: `python scripts\check-macos-support-gate.py`; focused pytest and compileall |
| 2026-06-26 | Refreshed GitHub #116 body after merged-PR gate fixes so the open issue itself points to current `main` and the updated final gate instead of stale `0717f45`/PR-ready wording. | `docs/current_status.md`, GitHub #116 body | `gh issue view/edit 116`; `pytest tests\test_current_status_docs.py tests\test_macos_support_docs.py -q`; `git diff --check` |
| 2026-06-26 | Added artifact head SHA provenance to the #116 final Mac evidence path. The download helper writes `MACOS_VALIDATION_ARTIFACT_HEAD_SHA`, the report and generated GitHub evidence comment record `Artifact head SHA`, check-complete requires it, and the final gate compares it to the successful manual macOS workflow run. | `scripts/macos-download-validation-artifacts.sh`, `scripts/macos-manual-validation-report.sh`, `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/macos_support.md`, `docs/current_status.md` | RED/GREEN focused pytest; final focused pytest, full macOS support gate, compileall, `git diff --check` |
| 2026-06-26 | Re-scanned repo-side follow-up candidates after #116 remained external and fixed stale One-Click readiness wording that still described closed #138/#139 as current tracking. | `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; broad `rg` stale/TODO/disabled scan |
| 2026-06-26 | Tightened One-Click evidence README wording so completed #138/#139 artifacts are not framed as future evidence; templates now read as refresh shapes. | `reports/oneclick_readiness/README.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q` |
| 2026-06-26 | Refreshed #116 body after documentation-only main commits moved current HEAD; issue body now matches the latest final-gate handoff. | `docs/current_status.md`, GitHub #116 body | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; `gh issue view/edit 116` |
| 2026-06-26 | Hardened the #116 handoff against future doc-only commit drift by replacing the fixed current-head SHA in the issue body with latest-pushed-main wording and adding a gate check to reject hard-coded current head SHAs. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md`, GitHub #116 body | RED/GREEN: focused pytest and `python scripts\check-macos-support-gate.py` |
| 2026-06-26 | Hardened the #116 handoff against stale `Latest ... actions/runs/<id>` wording; fixed run URLs are now reference evidence, and the gate rejects future reintroduction. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md`, GitHub #116 body | RED/GREEN: focused pytest and `python scripts\check-macos-support-gate.py` |
| 2026-06-26 | Refreshed stale top-level macOS focused test count after additional #116 gate coverage increased the focused suite to 51 tests. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final focused pytest |
| 2026-06-26 | Refreshed stale top-level full pytest count after accumulated test additions increased the suite to 1786 tests. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py -q`; final `pytest -q` |
| 2026-06-26 | Replaced misleading #116 `gh run list --workflow ... --branch main` operator guidance with event-filtered commands that were verified to return relevant workflow runs; gate now rejects the bad pattern. | `scripts/check-macos-support-gate.py`, `tests/test_rust_core_packaging.py`, `docs/current_status.md`, GitHub #116 body | RED/GREEN: focused pytest and `python scripts\check-macos-support-gate.py` |
| 2026-06-26 | Tightened #116 final evidence attachment wording so the finalizer and docs direct operators to attach the real-Mac bundle to #116 before closing it, while PR #117 remains only a mirrored traceability target. | `scripts/macos-manual-validation-report.sh`, `docs/macos_support.md`, `tests/test_rust_core_packaging.py`, `docs/current_status.md` | RED/GREEN: focused finalizer pytest; final: focused macOS/docs pytest, full #116 gate, compileall, `git diff --check` |
| 2026-06-26 | Audited the original Export table-selection question and recorded the current contract: the app can export individually selected tables today through `RustDumpExportDialog` -> `RustDumpExporter.export_tables` -> Rust Core `dump.run` `tables` filtering. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_export_table_selection_audit -q`; source/doc/GitHub issue scan |
| 2026-06-26 | Fixed TF-STATUS-024 after finding that direct DB Export/Import dialogs hard-coded the Rust DB Core endpoint host to `127.0.0.1`; both dialogs now preserve `connector.host` while tunnel flows still use their local connector host. | `src/ui/dialogs/db_dialogs.py`, `tests/test_db_dialogs.py`, `docs/current_status.md` | RED/GREEN: focused Export and Import direct-host pytest |
| 2026-06-27 | Analyzed the next remaining issue after main alignment. #116 still needs external real-Mac evidence, but the repo-side handoff had one drift: artifact download defaults still targeted PR #117 head after merge. Fixed TF-STATUS-025 so artifact lookup now follows PR head before merge and current merged main HEAD after PR #117 is merged. | `scripts/macos-download-validation-artifacts.sh`, `scripts/macos-manual-validation-report.sh`, `docs/macos_support.md`, `tests/test_rust_core_packaging.py`, `tests/test_macos_support_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_uses_local_head_after_pr_merge -q`; final: macOS/docs focused pytest, shell syntax, current-status tests, #116 gate skip-github, compileall, `git diff --check` |
| 2026-06-27 | Re-scanned disabled-feature docs after #116 remained external and fixed TF-STATUS-026: `SCHEDULE.md` no longer mixes a hidden-feature warning with current public UI instructions. | `SCHEDULE.md`, `tests/test_schedule_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_schedule_docs.py -q`; final: schedule/current-status docs pytest, stale-phrase scan, compileall, `git diff --check` |
| 2026-06-27 | Re-scanned One-Click readiness wording and fixed TF-STATUS-027: docs now distinguish the current backup-confirmed `engine_innodb` real-execution path from unsupported broad production automatic remediation and production charset/collation execution. | `docs/oneclick_readiness.md`, `tests/test_oneclick_readiness_docs.py`, `docs/current_status.md` | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py::test_oneclick_readiness_distinguishes_limited_real_execution_from_broad_production_support -q`; final: One-Click/current-status docs pytest, compileall, `git diff --check` |
| 2026-06-27 | Refreshed TF-STATUS-028 after rerunning the full Python suite. The current suite is now superseded by `1827 passed, 5 warnings`, replacing the stale `1786 passed` handoff count. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_does_not_keep_stale_full_pytest_count -q`; final: `pytest -q`, docs pytest, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-029 after noticing the top verification table still said `Verified On 2026-06-26` while containing a 2026-06-27 full pytest count. The section now describes a current baseline with preserved broader rows. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_current_baseline_section_is_not_stale_dated -q`; final: current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Re-audited current main and the next remaining issue. #116 is the only open GitHub issue, #116 repo-side gates pass, macOS focused tests now pass at 53 tests, and the Rust Core boundary scan found no new repo-side baseline violation; legacy-shaped DB connector names currently route through Rust Core shims. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: `pytest tests\test_current_status_docs.py::test_current_status_records_current_main_next_issue_reaudit -q`; final: #116 gates, macOS/docs focused pytest, current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Refreshed the top baseline counts after adding current-status re-audit coverage. The current full Python suite is now superseded by the 1827-test run, and the current macOS focused suite is now superseded by the 53-test run. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: stale-count current-status pytest; final: `pytest -q`, current-status pytest, macOS/docs focused pytest, compileall, `git diff --check` |
| 2026-06-27 | Removed a duplicate `--skip-github` row from the focused verification table and added a current-status regression so future focused verification command rows stay unique. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: duplicate-row current-status pytest; final: current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Merged duplicate `tunnelforge-core service.hello` rows in the current baseline table and added a regression so current baseline command rows stay unique. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: baseline duplicate-row current-status pytest; final: current-status pytest, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-034 after finding legacy-branded Export/Import context-menu wording on the Rust Core path; handlers and labels now use Rust DB Core naming, with a focused source-level regression and refreshed full-suite count. | `src/ui/main_window.py`, `tests/test_main_window_export_import_labels.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: Export/Import label pytest and current-status pytest; final: `pytest -q`, focused docs/UI pytest, compileall, `git diff --check`, #116 gate checks |
| 2026-06-27 | Fixed TF-STATUS-035 after finding One-Click disabled-real-execution fallback copy still pointed at closed #138; the fallback now describes real execution as disabled in this build and keeps dry-run preview wording current. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: One-Click tooltip/current-status pytest; final: `pytest -q`, focused One-Click/current-status pytest, compileall, `git diff --check`, #116 gates |
| 2026-06-27 | Fixed TF-STATUS-036 after finding the One-Click module docstring still overpromised full automatic migration; it now describes Rust DB Core dry-run default and limited backup-confirmed real execution. | `src/ui/dialogs/oneclick_migration_dialog.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: One-Click docstring/current-status pytest; final: `pytest -q`, focused One-Click/current-status pytest, compileall, `git diff --check`, #116 gates |
| 2026-06-27 | Fixed TF-STATUS-037 after finding stale Windows installer version examples in `BUILD.md`; output/test paths now use `{version}` and the Inno snippet uses `{#MyAppVersion}`. | `BUILD.md`, `tests/test_build_docs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: build-doc/current-status pytest; final: `pytest -q`, focused build-doc/current-status pytest, compileall, `git diff --check`, #116 gates |
| 2026-06-27 | Fixed TF-STATUS-038 after finding that #116 final gate manual workflow lookup still targeted PR #117 head after merge while artifact download/report SHA policy had moved to current merged main HEAD. | `scripts/check-macos-support-gate.py`, `docs/macos_support.md`, GitHub #116 body, `tests/test_rust_core_packaging.py`, `tests/test_macos_support_docs.py`, `tests/test_current_status_docs.py`, `docs/current_status.md` | RED/GREEN: manual workflow head-policy pytest, macOS support docs pytest, current-status pytest; #116 body updated and full gate rechecked |
| 2026-06-27 | Recorded TF-STATUS-039 after a post-merge next-issue re-audit found no new repo-side issue: #116 is still the only open GitHub issue, full #116 gates pass, and SQL editor execution also routes through Rust Core connector shims. | `docs/current_status.md`, `tests/test_current_status_docs.py` | RED/GREEN: post-merge current-status pytest; final: current-status pytest, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Created GitHub #142 and TF-STATUS-040 after finding a separate repo-side Rust Core baseline gap: the legacy Auto-Fix Wizard can still execute DB mutations through Python-owned fix logic. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #142 | RED/GREEN: legacy Auto-Fix current-status pytest; final: current-status pytest, issue scan, compileall, `git diff --check` |
| 2026-06-27 | Fixed TF-STATUS-040 / GitHub #142 by making the legacy Auto-Fix Wizard dry-run/manual SQL only and fail-closing `FixWizardWorker` when `dry_run=False` is requested. | `src/ui/dialogs/fix_wizard_dialog.py`, `src/ui/workers/fix_wizard_worker.py`, `src/core/i18n.py`, `tests/test_fix_wizard_dialog.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #142 | RED/GREEN: legacy Auto-Fix dialog/worker pytest and current-status pytest; final: full pytest, #116 gates, compileall, `git diff --check` |
| 2026-06-27 | Analyzed the next open issue after #142 closure. #116 is still the only open GitHub issue; normal repo-side gate passes, while `--final` fails because the real-Mac report and current-main manual workflow_dispatch evidence are not present. | `docs/current_status.md`, `tests/test_current_status_docs.py`, GitHub #116 | RED/GREEN: post-#142 current-status pytest; final: #116 gate, expected-failing final gate, current-status pytest, full pytest, compileall, `git diff --check` |
| 2026-06-27 | Created and fixed TF-STATUS-041 / GitHub #143 after finding that the underlying legacy Auto-Fix core APIs still accepted `dry_run=False` after #142 closed the user-visible worker path. | `src/core/migration_fix_wizard.py`, `tests/test_migration_fix_wizard.py`, `tests/test_current_status_docs.py`, `docs/current_status.md`, GitHub #143 | RED/GREEN: legacy core mutation API pytest and current-status pytest; final: full pytest, #116 gate, compileall, `git diff --check` |
