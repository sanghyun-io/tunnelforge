# Final Fix F1 Report

Status: DONE

Commit subject: `ci: PR 보안 게이트와 WebSetup self-check 강화`

Critical/Important follow-up subject:
`ci: F1 보안 게이트 terminal DAG 수정`

Base: `e0e9c7a8f79932f1cb0bf47c0341c50b73651d41`

## Scope

- Reduced workflow-default permissions to `contents: read` and made every
  PR-head regression checkout SHA-pinned, read-only, and
  `persist-credentials: false`.
- Moved write permissions to the two jobs that need them. Both checkout the PR
  base SHA; `version-bump` reads the PR head with `git show`/an alternate index,
  runs only the base-owned bump script, creates a commit tree without checking
  out PR code, and mints the App token only immediately before push.
- Renamed pre-bump label logic to `version-validation`; `version-bump` now
  depends on that internal job.
- Made required context `version-gate` terminal. It uses `if: always()`, waits
  for all regression jobs, validation, and `version-bump`, and accepts a skipped
  bump only when validation explicitly reports an empty bump type.
- Changed the macOS support tracking job to checkout and execute only the
  trusted PR base. PR-head regression jobs retain no credentials and expose no
  token, secret, or `GH_TOKEN` environment.
- Added `cargo test --manifest-path migration_core/Cargo.toml` after the static
  Rust Core regression script.
- Kept the Windows Rust build and full `pytest -q`, then added PyInstaller
  WebSetup build, artifact existence validation, frozen `--self-check`, exit
  validation, and exact marker validation.
- Added source/frozen-compatible bootstrapper `--self-check`. It validates
  required import APIs, shared integrity symbols and size contract, and both
  certifi/requests CA lookup paths without constructing Tk or using the network.
- Updated CI, bootstrapper integrity, and packaging regression contracts. Status
  documentation was intentionally not changed.

## RED/GREEN

- Initial CI/bootstrapper RED: `7 failed, 1 passed, 64 deselected`; failures
  covered global write permissions, missing job permissions/aggregate needs,
  missing Cargo and frozen checks, and missing source self-check.
- Required-import RED: `1 failed, 65 deselected` because a missing
  `requests.get` contract was not detected.
- Required-import GREEN: `3 passed, 63 deselected`.
- Final focused GREEN:
  `pytest -q tests/test_ci_workflows.py tests/test_bootstrapper_integrity.py tests/test_rust_core_packaging.py`
  reported `124 passed in 45.47s`.
- Critical/Important follow-up RED: `3 failed, 5 passed`; failures reproduced
  PR-head macOS gate execution, missing `version-validation`, and a non-terminal
  required context.
- Critical/Important follow-up GREEN:
  `pytest -q tests/test_ci_workflows.py tests/test_rust_core_packaging.py`
  reported `59 passed in 47.88s`.

## Verification Evidence

- `pyinstaller --noconfirm bootstrapper/bootstrapper.spec`: exit 0; produced
  `dist/TunnelForge-WebSetup.exe` with certifi and Tk hooks included.
- Source self-check: exit 0, exact marker
  `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK`.
- Frozen WebSetup self-check: exit 0, exact marker
  `TUNNELFORGE_WEBSETUP_SELF_CHECK_OK` captured from PowerShell.
- Focused Python compile check for the changed Python/test files: exit 0.
- `git diff --check`: pass.
- Follow-up workflow tests dynamically enumerate every PR-head checkout and
  reject token/secret exposure; terminal DAG and bump skip/success disposition
  are asserted explicitly.
- Full suite was not run, per assignment.

## Result

DONE
