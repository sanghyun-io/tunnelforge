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

TunnelForge is in a strong build/test state. The active architecture baseline
is Rust Core ownership of DB operations through `tunnelforge-core`, with
Python/PyQt responsible for UI, orchestration, signals, and dialogs.

Open GitHub issue #116 remains external: its remaining unchecked criterion is
real operator Mac validation evidence. GitHub issues #137 through #141 closed
the current One-Click readiness sequence: dry-run preview, limited
`deprecated_engine -> engine_innodb` real execution, charset/collation supplied
contract execution, PyQt-triggered charset contract derivation, and
display-only `int_display_width` skip policy. No repo-side One-Click follow-up
issue is currently open; track each additional automatic-fix class as a
separate issue before implementation.

## Verified On 2026-06-26

Commands run locally:

| Check | Result |
| --- | --- |
| `git status --short --branch` | `## main...origin/main`, no local changes before this document |
| `pytest -q` | PASS, 1729 passed, 5 warnings |
| `cargo test --manifest-path migration_core\Cargo.toml` | PASS, 166 lib tests, JSONL CLI, live roundtrip, and non-ignored stress tests |
| `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS |
| `python -m compileall -q main.py src tests scripts` | PASS |
| `git diff --check` | PASS |
| `tunnelforge-core service.hello` | PASS, reports `dump.run`, `dump.import`, migration commands |
| `cargo test --manifest-path migration_core\Cargo.toml --test live_roundtrip -- --nocapture` | PASS, 6 live container MySQL/PostgreSQL smoke tests |
| `pytest tests\test_live_ui_migration_capture.py tests\test_live_ui_migration_evidence.py -q` | PASS, capture and validator tests |
| `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS |
| `python scripts\check-macos-support-gate.py --skip-github` | PASS |
| `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS, 47 passed |
| `tunnelforge-core service.hello` | PASS, advertises `oneclick.*` commands; PyQt exposes One-Click with Dry-run default and limited backup-gated `engine_innodb` real execution |

Version references are aligned at `2.1.6` across:

- `src/version.py`
- `pyproject.toml`
- `installer/TunnelForge.iss`

## Verification Log

| Date | Scope | Command | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-06-26 | One-Click closed-issue wording drift | RED/GREEN: `pytest tests\test_oneclick_readiness_docs.py -q`; `rg -n "TODO|FIXME|XXX|HACK|NotImplemented|raise NotImplementedError|pass\s*$" src tests scripts docs README.md README.ko.md BUILD.md SCHEDULE.md`; `rg -n "not yet supported|pending|future|disabled|hidden|preview|manual|not implemented|unsupported|준비|미지원|비활성|숨김|수동|remaining|still needs|still requires|open issue|blocked" docs README.md README.ko.md BUILD.md SCHEDULE.md src tests scripts`; `rg -n "#1(1[0-9]|2[0-9]|3[0-9]|4[0-9])|TF-STATUS-[0-9]+|Next action:" docs README.md README.ko.md BUILD.md SCHEDULE.md reports scripts tests src` | PASS | Fresh repo-side scan found stale current-tense One-Click tracking wording for closed #138/#139; readiness doc now states #137-#141 are completed and no One-Click follow-up issue is open |
| 2026-06-26 | macOS artifact head SHA provenance | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_validation_artifact_download_script_writes_env_file -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_check_complete_rejects_missing_artifact_metadata -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_checks_report_artifact_head_sha -q`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate -q`; `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_script_accepts_local_final_report tests\test_rust_core_packaging.py::test_macos_support_gate_script_checks_report_artifact_workflow_run tests\test_rust_core_packaging.py::test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate -q` | PASS | Final macOS evidence and generated GitHub evidence comment now record and gate-check the artifact workflow head SHA separately from the report Git SHA |
| 2026-06-26 | GitHub #116 handoff body refresh | `gh issue view 116 --json body`; `gh issue edit 116 --body-file <temp>`; `gh issue view 116 --json body --jq .body` | PASS | #116 Current Evidence now points operators at current `main` / gate head `6da13f7` and no longer says PR #117 still needs to be marked ready |
| 2026-06-26 | macOS final report SHA after PR #117 merge | RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_uses_local_head_for_final_report_after_pr_merge -q`; RED/GREEN: `pytest tests\test_macos_support_docs.py -q`; `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state tests\test_rust_core_packaging.py::test_macos_support_gate_script_accepts_local_final_report tests\test_rust_core_packaging.py::test_macos_support_gate_script_rejects_report_from_different_git_sha -q`; `python scripts\check-macos-support-gate.py` | PASS | Final gate now expects the current merged main HEAD for report Git SHA after PR #117 is merged, instead of the stale PR head |
| 2026-06-26 | macOS support gate after PR #117 merge | `python scripts\check-macos-support-gate.py` failed before fix because merged PR #117 reports `mergeStateStatus=UNKNOWN`; RED/GREEN: `pytest tests\test_rust_core_packaging.py::test_macos_support_gate_accepts_merged_pr_with_unknown_merge_state -q`; `python scripts\check-macos-support-gate.py`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `python -m compileall -q scripts\check-macos-support-gate.py tests\test_rust_core_packaging.py`; `git diff --check` | PASS | Full GitHub #116 gate now accepts merged PR #117 while still checking issue state and status checks |
| 2026-06-26 | Current status stale handoff scan | `rg -n "TODO|FIXME|XXX|HACK|NotImplemented|raise NotImplementedError|pass\s*$" src tests scripts docs README.md README.ko.md SCHEDULE.md`; `rg -n "not yet supported|pending|future|disabled|hidden|preview|manual|not implemented|unsupported|준비|미지원|비활성|숨김|수동" docs README.md README.ko.md SCHEDULE.md src tests`; `rg -n "GitHub issue #[0-9]+ now tracks|next actionable|remaining unchecked|should remain open|still requires|still needs|TODO" docs README.md README.ko.md reports scripts tests`; `pytest tests\test_current_status_docs.py -q` RED/GREEN | PASS | Found no new repo-side issue beyond #116; corrected stale top-handoff wording that still presented closed #140 as current work |
| 2026-06-26 | Current status summary consistency and next issue analysis | `pytest tests\test_current_status_docs.py -q` RED/GREEN; `pytest tests\test_current_status_docs.py tests\test_oneclick_readiness_docs.py -q`; `gh issue list --state open --limit 30 --json number,title,labels,updatedAt,url`; `gh issue view 116 --json number,title,state,body,labels,comments,url,updatedAt`; `python scripts\check-macos-support-gate.py --skip-github`; `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q`; `git diff --check` | PASS | Summary now matches GitHub state: #116 is the only open issue and #137-#141 are closed One-Click readiness work; no additional repo-side #116 gap found |
| 2026-06-26 | One-Click PyQt charset derivation evidence | `pytest tests\test_oneclick_charset_derivation_evidence.py -q` RED/GREEN; `pytest tests\test_oneclick_charset_derivation_capture.py -q` RED/GREEN; `pytest tests\test_oneclick_charset_capture.py tests\test_oneclick_charset_evidence.py tests\test_oneclick_charset_derivation_capture.py tests\test_oneclick_charset_derivation_evidence.py -q`; `cargo build --manifest-path migration_core\Cargo.toml --release`; `python scripts\capture-oneclick-charset-derivation-evidence.py --seed-local-container --mysql-container tf-live-mysql --mysql-host 127.0.0.1 --mysql-port 3406 --mysql-user root --mysql-password test --schema tf_oneclick_derive_charset --output reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; `python scripts\validate-oneclick-charset-derivation-evidence.py reports\oneclick_readiness\oneclick-charset-derivation-evidence.json`; `$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_DERIVATION_EVIDENCE='1'; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | #140 local evidence proves PyQt-triggered Rust Core derivation feeds `oneclick.run dry_run=false` and converts 2 FK-connected local tables |
| 2026-06-26 | One-Click follow-up issue split | `rg -n "invalid_date|zerofill_usage|float_precision|int_display_width|enum_empty_value|manual|skip|oneclick_issues_from_inspection|oneclick_recommendations|oneclick_auto_fix_option" migration_core\src\lib.rs docs\oneclick_readiness.md tests docs\current_status.md`; `gh issue create` created #141 | PASS | `int_display_width` skip semantics are now tracked separately from closed #140 |
| 2026-06-26 | One-Click `int_display_width` skip policy | `pytest tests\test_oneclick_readiness_docs.py -q` RED/GREEN; `pytest tests\test_oneclick_readiness_docs.py tests\test_oneclick_rust_core_gate.py -q`; `cargo test --manifest-path migration_core\Cargo.toml oneclick_live_inspection_does_not_synthesize_int_display_width_skip --lib`; `cargo test --manifest-path migration_core\Cargo.toml oneclick --lib`; `cargo fmt --manifest-path migration_core\Cargo.toml --check`; `git diff --check` | PASS | #141 policy is now explicit: externally supplied `skip` is display-only and Rust Core live One-Click does not synthesize or execute this class |
| 2026-06-26 | Main merge/status and then-current next issue analysis | `git fetch origin --prune`; `git status --short --branch`; `gh issue list --state open --limit 20 --json number,title,labels,updatedAt,assignees,url`; `gh issue view 140 --comments --json number,title,state,body,comments,labels,url,updatedAt`; `gh issue view 116 --json number,title,state,body,labels,url,updatedAt`; `rg -n "TF-STATUS-022|#140|derive_charset|oneclick.derive_charset|charset_contracts|OneClickMigrationWorker|derive_oneclick_charset_contracts" ...` | PASS | Historical row: at that point `main` was aligned with `origin/main`, #140 was the next actionable in-repo issue, and #116 remained external real-Mac evidence; #140 is now closed |
| 2026-06-26 | Current main full Python suite | `pytest -q` | PASS | 1729 passed, 5 warnings |
| 2026-06-26 | Current main Rust core tests | `cargo test --manifest-path migration_core\Cargo.toml` | PASS | 166 lib tests, JSONL CLI test, 6 live-roundtrip tests, 2 non-ignored stress tests, doctests |
| 2026-06-26 | Current main Rust release build | `cargo build --manifest-path migration_core\Cargo.toml --release` | PASS | Produced release Rust core binary |
| 2026-06-26 | Current main Python syntax | `python -m compileall -q main.py src tests scripts` | PASS | No compile errors |
| 2026-06-26 | Current main live UI evidence validator | `python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json` | PASS | 2 directions and 12,000,000 rows checked |
| 2026-06-26 | Current main Rust performance evidence validator | `python scripts\validate-rust-core-performance-evidence.py` | PASS | 4 files and 11,000,000 rows proven |
| 2026-06-26 | Current main optional evidence regression gate | `RUST_CORE_REQUIRE_PERF_EVIDENCE=1; RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE=1; powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1` | PASS | Requires both archived Rust performance evidence and live UI migration evidence |
| 2026-06-26 | Current main macOS support gate | `python scripts\check-macos-support-gate.py --skip-github` | PASS | Repository-side macOS support tracking checks pass without final real-Mac evidence |
| 2026-06-26 | Current main macOS focused tests | `pytest tests\test_rust_core_packaging.py tests\test_macos_support_docs.py -q` | PASS | 47 passed |
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

## Recommended Execution Order

1. Keep TF-STATUS-008 / GitHub #116 tracked separately because it requires real
   operator Mac validation evidence.
2. Track additional One-Click automatic fix classes as separate GitHub issues
   before implementation.

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
