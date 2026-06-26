# Autonomous Next Work Context

Task statement: continue autonomously on TunnelForge, find the next useful
repo-side work, and avoid waiting for explicit step-by-step direction.

Desired outcome: keep `main` healthy, identify real repo-side issues before
creating work, fix and verify confirmed issues, and record status so any later
session can continue from the same evidence.

Known facts/evidence at snapshot creation:

- `main` was aligned with `origin/main` at the start of this continuation.
- The only open GitHub issue is #116, macOS Support M6.
- `python scripts\check-macos-support-gate.py` passes on current main.
- `python scripts\check-macos-support-gate.py --final` fails because final
  real-Mac evidence is missing: no completed manual validation report under
  `build/`, and no successful manual `macOS App Validation` workflow_dispatch
  run exists for current merged main HEAD.
- `scripts\rust-core-regression-gate.ps1` passes.
- Source/package/installer versions are aligned at `2.1.8`; latest release is
  `v2.1.7`, so `2.1.8` is the next-unreleased version.
- A post-#151 status coverage refresh created GitHub #152 / TF-STATUS-053 and
  refreshed current full-suite evidence to `1835 passed, 5 warnings`.

Constraints:

- Rust Core migration remains the architecture baseline.
- Do not reintroduce direct Python DB driver hot paths, external dump tool
  paths, or retired helper aliases in product paths.
- `docs/current_status.md` is the canonical handoff and must be updated for
  status-changing investigations or fixes.
- Use tests/gates as evidence; create GitHub issues for confirmed repo-side
  issues before fixing them when practical.

Unknowns/open questions:

- Whether any remaining direct DB-looking calls are product-path violations or
  only Rust Core shim use, tests, or evidence-capture support.
- Whether current-status full-suite counts are stale after future test changes.
- Whether #116 real-Mac evidence can be generated in this environment; current
  evidence says it requires an external real operator Mac.

Likely codebase touchpoints:

- `docs/current_status.md`
- `tests/test_current_status_docs.py`
- `scripts/check-macos-support-gate.py`
- `scripts/rust-core-regression-gate.ps1`
- `src/core/db_core_service.py`
- `src/core/db_connector.py`
- `src/ui/dialogs/sql_editor_dialog.py`
- `src/ui/workers/test_worker.py`
- `src/core/scheduler.py`
