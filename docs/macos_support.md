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
- Startup: LaunchAgent registration creates and removes `~/Library/LaunchAgents/io.sanghyun.tunnelforge.plist`.
- Updates: release asset selection prefers the current Mac architecture's DMG and does not execute DMG/ZIP files as programs.

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
- Parse `.github/workflows/release.yml` and `.github/workflows/macos-app.yml` as YAML.
- The `macOS App Validation` workflow runs on pull requests and manual dispatch, builds `arm64` and `x86_64` `.app` packages, and uploads DMG/ZIP artifacts for inspection.

These checks require macOS:

- `bash scripts/build-macos.sh`
- `bash scripts/package-macos.sh`
- Optional signing/notarization with `APPLE_CODESIGN_IDENTITY`, `APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_SPECIFIC_PASSWORD`.

## Final Manual Validation

Final Manual Validation must happen after all implementation milestones are complete and before calling macOS support production-ready.

Run on macOS:

1. Build Rust Core and app bundle with `bash scripts/build-macos.sh`.
2. Package with `bash scripts/package-macos.sh`.
3. Launch `dist/TunnelForge.app`.
4. Confirm `tunnelforge-core` starts from inside the app.
5. Create and close an SSH tunnel.
6. Test MySQL and PostgreSQL DB connections.
7. Run Export/Import on a disposable database.
8. Run Migration smoke flow: inspect, preflight, plan, migrate, verify, resume.
9. Enable and disable startup, then inspect the LaunchAgent file.
10. Check settings, logs, SQL history, migration state, analysis, and rollback files under macOS user directories.
11. Open a downloaded DMG through the update UI and confirm it does not try to execute it directly.
12. Install from DMG into Applications and launch from there.
13. If distributing outside internal testing, verify codesign, notarization, and Gatekeeper behavior.
