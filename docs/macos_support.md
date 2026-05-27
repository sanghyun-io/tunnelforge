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
- Startup: LaunchAgent registration creates and removes `~/Library/LaunchAgents/io.sanghyun.tunnelforge.plist`, records launch stdout/stderr under `~/Library/Logs/TunnelForge/`, and sets `WorkingDirectory` to the app executable directory.
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
- The `Version Gate` workflow includes a macOS validation matrix so an existing default-branch PR workflow can build `arm64` and `x86_64` packages, run `--ui-smoke-check` against source, `.app`, DMG, copied-DMG install, `/Applications install smoke`, and ZIP paths, smoke-test LaunchAgent plist registration for the copied and `/Applications` installs, and upload DMG/ZIP artifacts plus `.sha256` checksums for inspection.
- The `Version Gate` workflow also runs `python scripts/check-macos-support-gate.py --skip-pr-checks` so M0-M5 issue closure and #116/M6 tracking are checked on every macOS support PR update without waiting for final real-Mac evidence.
- The standalone `macOS App Validation` workflow provides the same macOS package validation path for PR/manual runs once GitHub recognizes the workflow from the repository default branch. Its `workflow_dispatch` path falls back to `github.sha` when no pull request SHA exists, and can run signed/notarized macOS validation before a tag release when the Apple Developer ID and notarization secrets are configured.
- The release workflow repeats `--ui-smoke-check` against the source app, built `.app`, mounted DMG app, copied DMG install app, `/Applications` installed app, and extracted ZIP app before uploading macOS release assets and `.sha256` checksums. PR validation also runs `scripts/smoke-macos-launchagent.sh` against the copied DMG install and `scripts/smoke-macos-applications-install.sh` against `/Applications/TunnelForge.app` to verify LaunchAgent plist structure and log paths on hosted macOS.
- `bash scripts/macos-download-validation-artifacts.sh` uses `gh run download` to fetch the latest successful manual `workflow_dispatch` `macOS App Validation` artifacts for the current PR head, or a specific `--run-id`, then verifies downloaded DMG/ZIP files against their `.sha256` files. Use `--arch arm64` or `--arch x86_64` to fetch only one Mac architecture. Use `--write-env <file>` when you need a sourceable `MACOS_VALIDATION_ARTIFACT_*` provenance file for the manual validation report.

These checks require macOS:

- `bash scripts/build-macos.sh`
- `bash scripts/package-macos.sh`
- `bash scripts/validate-macos-release.sh` to run source `python main.py --ui-smoke-check`, build, package, and run `--ui-smoke-check` against the built app, mounted DMG app, copied DMG install app, and extracted ZIP app on a Mac.
- `bash scripts/macos-manual-validation-report.sh --download-artifacts --run-smoke` to download and checksum-verify the matching GitHub Actions artifacts for the current Mac architecture, record `Artifact workflow run`, artifact directory, and checksum status in the report, then create a timestamped Markdown report, smoke log, and system evidence log for the remaining manual SSH, DB, migration, LaunchAgent, update, install, signing, notarization, and Gatekeeper checks. When artifact download/checksum verification and release smoke pass, the generated report pre-checks those automated checklist items so the operator can focus on the remaining interactive checks. Set `MACOS_RELEASE_SMOKE_APPLICATIONS=1` when running this command to include the `/Applications` install smoke in the automated release smoke log. Use `--artifact-run-id <workflow-run-id>` only when you need to pin a specific workflow run. Use `--artifact-arch <arm64|x86_64|all>` only when overriding the current Mac architecture. Use `--artifact-output-dir <dir>` when you need explicit artifact provenance.
- `bash scripts/macos-manual-validation-report.sh --check-complete <report.md>` to fail the final gate when any required sections or required checklist items are missing, when any interactive section is missing a filled `Evidence:` note, when an `Evidence:` note still contains placeholder/TODO text instead of concrete observations, when the macOS version, Mac architecture, Artifact workflow run, Artifact checksum verification, Final app path, final app executable, or system evidence log metadata is missing, or when any checkbox, smoke result, smoke log file, overall result, or validator field is incomplete. The smoke log must include both the release smoke completion message and the successful /Applications install smoke completion message. The system evidence log must include `sw_vers`, `uname`, architecture, final app path, `codesign --verify`, and `spctl --assess` evidence. Use the exact report path printed by `--run-smoke`.
- `bash scripts/macos-manual-validation-report.sh --bundle-evidence <report.md>` to create a `build/macos-manual-validation-evidence-*.zip` bundle containing the completed report, smoke log, system evidence log, and SHA256 manifest for PR or release attachment, plus a sibling `*.zip.sha256` checksum for the bundle itself. Use `--evidence-bundle <zip>` to choose a specific output path.
- `bash scripts/macos-manual-validation-report.sh --finalize <report.md>` to run the completed-report check, create the evidence zip and checksum, run `scripts/check-macos-support-gate.py --final`, write a GitHub evidence comment Markdown file with the report Git SHA, Artifact workflow run, and Evidence bundle SHA256, and print the exact report/log/system-evidence/comment/zip/checksum attachment paths. Add `--post-github-comment` to post that generated comment to #116 and PR #117 after finalization. Use `--skip-github` only for offline local rehearsal.
- `python scripts/check-macos-support-gate.py --final --report build/macos-manual-validation-report-*.md --bundle build/macos-manual-validation-evidence-*.zip` to verify M0-M5 are closed, #116 is assigned to M6, PR #117 checks are green, the report Git SHA matches the current PR head, and the real-Mac report/log/system-evidence/bundle evidence is complete. The Python gate accepts explicit paths or glob patterns and selects the newest match when multiple files exist.
- Optional local signing/notarization with `APPLE_CODESIGN_IDENTITY`, `APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_SPECIFIC_PASSWORD`.
- Optional GitHub Release signing/notarization secrets:
  - `APPLE_CODESIGN_CERTIFICATE_P12_BASE64`: base64-encoded Developer ID Application `.p12` certificate.
  - `APPLE_CODESIGN_CERTIFICATE_PASSWORD`: password for the `.p12` certificate.
  - `APPLE_CODESIGN_IDENTITY`: optional explicit Developer ID Application identity name; if omitted, the release workflow discovers the first Developer ID Application identity from the imported certificate.
  - `APPLE_CODESIGN_KEYCHAIN_PASSWORD`: optional temporary keychain password; if omitted, the release workflow generates one.
  - `APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_SPECIFIC_PASSWORD`: Apple notarization credentials passed to `notarytool`.
- When notarization credentials are present, `scripts/package-macos.sh` submits a temporary app ZIP for notarization, staples the returned ticket to the `.app`, validates the stapled `.app`, then creates the final ZIP distribution from that stapled `.app`. It also notarizes, staples, and validates the DMG distribution.
- For a pre-release hosted check of the Apple secret path, manually run `.github/workflows/macos-app.yml` with `workflow_dispatch` on the release branch. When the signing certificate and notarization credentials are available, the workflow imports the Developer ID certificate into a temporary keychain, packages signed/notarized artifacts, then verifies the `.app` and DMG with `codesign --verify`, `spctl --assess`, and `xcrun stapler validate`.
- After that hosted check passes, run `MACOS_RELEASE_SMOKE_APPLICATIONS=1 bash scripts/macos-manual-validation-report.sh --download-artifacts --run-smoke` on the validator Mac to download the latest successful signed/notarized artifacts for the PR head and current Mac architecture, verify checksums, record artifact provenance, and start the final manual report in one command.

## Final Manual Validation

Final Manual Validation must happen after all implementation milestones are complete and before calling macOS support production-ready.

Run on macOS:

1. Run `MACOS_RELEASE_SMOKE_APPLICATIONS=1 bash scripts/macos-manual-validation-report.sh --download-artifacts --run-smoke` to download the latest successful signed/notarized GitHub Actions artifacts for the PR head and current Mac architecture, verify DMG/ZIP checksums, generate a report, smoke log, and system evidence log under `build/`, record the artifact provenance in the report, and pre-check the automated smoke/download checklist items that passed.
2. If artifact download was done separately, run `bash scripts/macos-download-validation-artifacts.sh --arch <arm64|x86_64> --write-env build/macos-validation-artifacts.env`, source the generated env file, then run `MACOS_RELEASE_SMOKE_APPLICATIONS=1 bash scripts/macos-manual-validation-report.sh --run-smoke`. Add `--run-id <workflow-run-id>` only when pinning a specific run.
3. Build Rust Core and app bundle with `bash scripts/build-macos.sh` if validating the build step separately.
4. Package with `bash scripts/package-macos.sh` if validating packaging separately.
5. Launch `dist/TunnelForge.app`.
6. Confirm `tunnelforge-core` starts from inside the app.
7. Create and close an SSH tunnel.
8. Test MySQL and PostgreSQL DB connections.
9. Run Export/Import on a disposable database.
10. Run Migration smoke flow: inspect, preflight, plan, migrate, verify, resume.
11. Enable and disable startup, then inspect the LaunchAgent file, `WorkingDirectory`, and `~/Library/Logs/TunnelForge/launchagent.{out,err}.log` paths.
12. Check settings, logs, SQL history, migration state, analysis, and rollback files under macOS user directories.
13. Open a downloaded DMG through the update UI and confirm it does not try to execute it directly.
14. Install from DMG into Applications and launch from there.
15. If distributing outside internal testing, verify codesign, notarization, and Gatekeeper behavior.
16. Mark every report checkbox complete, fill the `Evidence:` note in each interactive section with concrete observed behavior or file/log paths, remove placeholder/TODO text, confirm `Artifact workflow run` matches the downloaded manual workflow run, set `Artifact checksum verification` to `passed`, set `Overall result` to `passed`, fill `Validator`, and run `bash scripts/macos-manual-validation-report.sh --finalize <report.md> --post-github-comment`.
17. If finalizing manually instead, run `bash scripts/macos-manual-validation-report.sh --check-complete <report.md>`, then `bash scripts/macos-manual-validation-report.sh --bundle-evidence <report.md>`, then `python scripts/check-macos-support-gate.py --final --report <report.md> --bundle <evidence.zip>`.
18. Confirm the finalizer or Python gate reports that the GitHub tracking issues, PR checks, completed report, smoke log, system evidence log, and evidence bundle agree.
19. Use the generated `build/macos-final-validation-github-comment-*.md` GitHub evidence comment as the issue/PR update body after attaching the files; it includes the report Git SHA, Artifact workflow run, and Evidence bundle SHA256 for auditability.
20. Attach the completed `build/macos-manual-validation-report-*.md` report, `build/macos-release-smoke-*.log` smoke log, `build/macos-system-evidence-*.log` system evidence log, `build/macos-manual-validation-evidence-*.zip` bundle with its embedded SHA256 manifest, and sibling `*.zip.sha256` checksum to the PR or release checklist before closing the final macOS gate.
