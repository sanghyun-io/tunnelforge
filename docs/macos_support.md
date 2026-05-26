# macOS Support Plan

This document tracks the repository-level work required to make TunnelForge usable on macOS while preserving the existing Windows release path.

GitHub tracking:

- #110 - Compatibility baseline
- #111 - Platform abstraction
- #112 - Source-run MVP
- #113 - App bundle packaging
- #114 - GitHub Actions build
- #115 - Platform-aware updates
- #116 - LaunchAgent and distribution quality

## Support Scope

- Target OS baseline: macOS 13+.
- CPU support goal: Apple Silicon and Intel. GitHub Actions builds separate `arm64` and `x86_64` macOS release artifacts on `macos-14` and `macos-15-intel`; final architecture coverage must be confirmed on release hardware or CI artifacts.
- Build baseline: `scripts/build-macos.sh` defaults `MACOSX_DEPLOYMENT_TARGET` to `13.0`.
- Existing Windows support remains in scope. Windows 10+ installer, WebSetup recovery flow, and Rust DB Core packaging must continue to work.
- Final Manual Validation is intentionally separate from codebase verification. The repository can prove build scripts, package wiring, and regression tests, but actual macOS usability requires running the built app on macOS.

## Feature Parity Checklist

Each release candidate must validate these user-visible behaviors:

- App launch from source: `python main.py`.
- App launch from package: `dist/TunnelForge.app`.
- Resource loading: app icon and bundled assets resolve without `.ico` assumptions.
- SSH tunnel: create, monitor, reconnect, and close a tunnel.
- Database connection: MySQL and PostgreSQL connection tests through Rust DB Core.
- Rust DB Core: `tunnelforge-core` is discoverable in source, PyInstaller, and installed app contexts.
- Export/Import: Rust dump export and import workflows complete on test databases.
- Migration: cross-engine inspect, preflight, plan, migrate, verify, and resume flows are callable.
- Settings: config, encryption key, SQL history, migration state, analysis, rollback, and logs use macOS-appropriate user paths.
- Startup: LaunchAgent registration creates and removes `~/Library/LaunchAgents/io.sanghyun.tunnelforge.plist` and records launch stdout/stderr under `~/Library/Logs/TunnelForge/`.
- Updates: release asset selection prefers the current Mac architecture's DMG, publishes `.sha256` checksums for DMG/ZIP packages, and does not execute DMG/ZIP files as programs.

## Windows Regression Gates

These checks must continue to pass before merging macOS support changes:

- `pytest`
- `cargo test --manifest-path migration_core/Cargo.toml`
- `python -m compileall main.py src tests`
- Windows PyInstaller spec keeps including `tunnelforge-core.exe`.
- Windows installer flow still builds through `scripts/build-installer.ps1`.
- In-app Windows update flow still selects `TunnelForge-Setup-*.exe` and excludes `TunnelForge-WebSetup.exe` as the offline installer asset.

## macOS Codebase Verification

These checks are valid on any development host unless noted:

- `pytest tests/test_platform_paths.py tests/test_platform_integration.py tests/test_resources.py tests/test_update_downloader.py tests/test_rust_core_packaging.py tests/test_macos_support_docs.py`
- `bash -n scripts/build-macos.sh scripts/package-macos.sh`
- Parse `tunnel-manager.spec` as Python syntax.
- Parse `.github/workflows/release.yml`, `.github/workflows/macos-app.yml`, and `.github/workflows/version-gate.yml` as YAML.
- The `Version Gate` workflow includes a macOS validation matrix so an existing default-branch PR workflow can build `arm64` and `x86_64` packages, run `--ui-smoke-check` against source, `.app`, DMG, copied-DMG install, and ZIP paths, smoke-test LaunchAgent plist registration for the copied install, and upload DMG/ZIP artifacts plus `.sha256` checksums for inspection.
- The `Version Gate` workflow also runs `python scripts/check-macos-support-gate.py --skip-pr-checks` so M0-M5 issue closure and #116/M6 tracking are checked on every macOS support PR update without waiting for final real-Mac evidence.
- The standalone `macOS App Validation` workflow provides the same macOS package validation path for PR/manual runs once GitHub recognizes the workflow from the repository default branch.
- The release workflow repeats `--ui-smoke-check` against the source app, built `.app`, mounted DMG app, copied DMG install app, and extracted ZIP app before uploading macOS release assets and `.sha256` checksums. PR validation also runs `scripts/smoke-macos-launchagent.sh` against the copied DMG install to verify LaunchAgent plist structure and log paths on hosted macOS.

These checks require macOS:

- `bash scripts/build-macos.sh`
- `bash scripts/package-macos.sh`
- `bash scripts/validate-macos-release.sh` to run source `python main.py --ui-smoke-check`, build, package, and run `--ui-smoke-check` against the built app, mounted DMG app, copied DMG install app, and extracted ZIP app on a Mac.
- `bash scripts/macos-manual-validation-report.sh --run-smoke` to create a timestamped Markdown report and smoke log for the remaining manual SSH, DB, migration, LaunchAgent, update, install, signing, notarization, and Gatekeeper checks.
- `bash scripts/macos-manual-validation-report.sh --check-complete <report.md>` to fail the final gate when any checkbox, smoke result, smoke log file, overall result, or validator field is incomplete. Use the exact report path printed by `--run-smoke`.
- `bash scripts/macos-manual-validation-report.sh --bundle-evidence <report.md>` to create a `build/macos-manual-validation-evidence-*.zip` bundle containing the completed report, smoke log, and SHA256 manifest for PR or release attachment, plus a sibling `*.zip.sha256` checksum for the bundle itself. Use `--evidence-bundle <zip>` to choose a specific output path.
- `bash scripts/macos-manual-validation-report.sh --finalize <report.md>` to run the completed-report check, create the evidence zip and checksum, run `scripts/check-macos-support-gate.py --final`, and print the exact report/log/zip/checksum attachment paths. Use `--skip-github` only for offline local rehearsal.
- `python scripts/check-macos-support-gate.py --final --report build/macos-manual-validation-report-*.md --bundle build/macos-manual-validation-evidence-*.zip` to verify M0-M5 are closed, #116 is assigned to M6, PR #117 checks are green, and the real-Mac report/log/bundle evidence is complete. The Python gate accepts explicit paths or glob patterns and selects the newest match when multiple files exist.
- Optional local signing/notarization with `APPLE_CODESIGN_IDENTITY`, `APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_SPECIFIC_PASSWORD`.
- Optional GitHub Release signing/notarization secrets:
  - `APPLE_CODESIGN_CERTIFICATE_P12_BASE64`: base64-encoded Developer ID Application `.p12` certificate.
  - `APPLE_CODESIGN_CERTIFICATE_PASSWORD`: password for the `.p12` certificate.
  - `APPLE_CODESIGN_IDENTITY`: optional explicit Developer ID Application identity name; if omitted, the release workflow discovers the first Developer ID Application identity from the imported certificate.
  - `APPLE_CODESIGN_KEYCHAIN_PASSWORD`: optional temporary keychain password; if omitted, the release workflow generates one.
  - `APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_SPECIFIC_PASSWORD`: Apple notarization credentials passed to `notarytool`.
- When notarization credentials are present, `scripts/package-macos.sh` submits a temporary app ZIP for notarization, staples the returned ticket to the `.app`, validates the stapled `.app`, then creates the final ZIP distribution from that stapled `.app`. It also notarizes, staples, and validates the DMG distribution.

## Final Manual Validation

Final Manual Validation must happen after all implementation milestones are complete and before calling macOS support production-ready.

Run on macOS:

1. Run `bash scripts/macos-manual-validation-report.sh --run-smoke` to generate a report and smoke log under `build/` while running the automated release smoke checks.
2. Build Rust Core and app bundle with `bash scripts/build-macos.sh` if validating the build step separately.
3. Package with `bash scripts/package-macos.sh` if validating packaging separately.
4. Launch `dist/TunnelForge.app`.
5. Confirm `tunnelforge-core` starts from inside the app.
6. Create and close an SSH tunnel.
7. Test MySQL and PostgreSQL DB connections.
8. Run Export/Import on a disposable database.
9. Run Migration smoke flow: inspect, preflight, plan, migrate, verify, resume.
10. Enable and disable startup, then inspect the LaunchAgent file and `~/Library/Logs/TunnelForge/launchagent.{out,err}.log` paths.
11. Check settings, logs, SQL history, migration state, analysis, and rollback files under macOS user directories.
12. Open a downloaded DMG through the update UI and confirm it does not try to execute it directly.
13. Install from DMG into Applications and launch from there.
14. If distributing outside internal testing, verify codesign, notarization, and Gatekeeper behavior.
15. Mark every report checkbox complete, set `Overall result` to `passed`, fill `Validator`, and run `bash scripts/macos-manual-validation-report.sh --finalize <report.md>`.
16. If finalizing manually instead, run `bash scripts/macos-manual-validation-report.sh --check-complete <report.md>`, then `bash scripts/macos-manual-validation-report.sh --bundle-evidence <report.md>`, then `python scripts/check-macos-support-gate.py --final --report <report.md> --bundle <evidence.zip>`.
17. Confirm the finalizer or Python gate reports that the GitHub tracking issues, PR checks, completed report, smoke log, and evidence bundle agree.
18. Attach the completed `build/macos-manual-validation-report-*.md` report, `build/macos-release-smoke-*.log` smoke log, `build/macos-manual-validation-evidence-*.zip` bundle with its embedded SHA256 manifest, and sibling `*.zip.sha256` checksum to the PR or release checklist before closing the final macOS gate.
