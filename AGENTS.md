# Repository Guidelines

## Project Structure & Module Organization
TunnelForge is a Python 3.9+ PyQt6 desktop app. `main.py` is the application entry point and `src/version.py` is the version source of truth. Core business logic lives in `src/core/`, database export/import code in `src/exporters/`, and UI code in `src/ui/` with dialogs under `src/ui/dialogs/`, workers under `src/ui/workers/`, and widgets under `src/ui/widgets/`. Tests are in `tests/` and generally mirror the module or feature they cover. Packaging assets live in `assets/`, installer configuration in `installer/`, release/build scripts in `scripts/`, and bootstrapper code in `bootstrapper/`.

## Current Project Memory
Rust Core migration is the active architecture baseline. Treat `tunnelforge-core` as the DB operation owner; Python/PyQt should stay focused on UI, orchestration, signals, and dialogs. Export/import uses `src/exporters/rust_dump_exporter.py` through Rust JSONL commands `dump.run` and `dump.import`. Cross-engine migration uses the Rust core service for inspect, preflight, plan, migrate, verify, and resume.

Do not reintroduce direct Python DB driver hot paths, external dump tool paths, or retired helper aliases in `src/`, tests, packaging, or user-facing docs. Packaging should include the single Rust DB core binary `tunnelforge-core(.exe)`.

## Distribution Policy
TunnelForge is distributed directly through GitHub Releases and is not planned
for Apple App Store registration. Do not make a paid Apple Developer account,
Developer ID certificate, or Apple notarization credentials a release
prerequisite. The default macOS release path builds unsigned arm64 and x86_64
DMG/ZIP artifacts and protects distribution integrity with SHA-256 checksum
files, GitHub Release asset digests, protected immutable release tags, and the
approved release workflow. Keep Apple signing/notarization support optional and
fail closed on partial Apple credential configuration; use it only if the
project's distribution policy explicitly changes later.

## Session Continuity & Status Tracking
`docs/current_status.md` is the canonical handoff and issue-tracking document. For any non-trivial investigation, implementation, verification, release, or documentation work, read it before deciding next steps. When a session discovers a new issue, changes an existing issue, runs meaningful verification, changes feature status, or resolves work, update `docs/current_status.md` in the same session.

Use stable issue IDs in the `TF-STATUS-###` format and do not renumber them. Do not mark an issue `closed` without fresh command evidence in that session. If a focused fix lands but broader verification or downstream work remains, use `fixed_pending_full_verify`. Keep the `Issue Tracker`, `Verification Log`, `Recommended Execution Order`, and `Session Log` aligned.

## Build, Test, and Development Commands
Create and activate a virtual environment before development:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
```

Run the app locally with `python main.py`. Run all Python tests with `pytest`; use `pytest tests/test_tunnel_engine.py` for a focused test file. For Rust core changes, run `cargo test --manifest-path migration_core\Cargo.toml` and `cargo build --manifest-path migration_core\Cargo.toml --release`. Build a standalone executable with `pyinstaller tunnel-manager.spec`. Build the Windows installer with `.\scripts\build-installer.ps1`, or add `-Clean` to remove previous build artifacts first.

## Coding Style & Naming Conventions
Follow the existing Python style: 4-space indentation, snake_case for modules, functions, and variables, PascalCase for classes, and descriptive names for UI actions and domain concepts. Keep business logic in `src/core/` and keep PyQt widget orchestration in `src/ui/`. Add comments only for non-obvious behavior; Korean UI text and comments are acceptable where consistent with nearby code.

## Testing Guidelines
Tests use `pytest` with helpers in `tests/conftest.py`. Name files `test_<feature>.py` and test functions `test_<expected_behavior>`. Add or update focused tests for changes to migration rules, SQL validation, connection handling, scheduling, GitHub reporting, and UI dialog behavior. Use `pytest --cov=src` when checking coverage for broader changes.

## Commit & Pull Request Guidelines
Recent history uses short imperative or conventional-style subjects, often with prefixes such as `Fix:`, `chore:`, or `Bump version to ...`; keep the first line concise and mention issue numbers when relevant, for example `Fix: prevent local_infile import error (#91)`. Pull requests should describe the user-visible change, list test results, link related issues, and include screenshots or screen recordings for UI changes.

## Security & Configuration Tips
Do not commit real credentials, private keys, database dumps, or production connection details. Use `.env.example` and `secrets/*.example` as templates only. Treat production database operations carefully and preserve existing confirmation and guard behavior.
