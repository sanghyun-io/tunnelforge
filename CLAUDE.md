# Language
**Allways Answer Korean**

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TunnelForge - Python PyQt6 GUI application for managing SSH tunnels and MySQL/PostgreSQL database operations. UI orchestration stays in Python/PyQt, while DB auth, schema inspection, SQL execution, dump/import, and cross-engine migration run through the Rust `tunnelforge-core` JSONL service.

## Current Project Memory

- Rust Core migration is the active architecture baseline. Treat `tunnelforge-core` as the DB operation owner and keep Python/PyQt focused on UI, orchestration, signals, and dialogs.
- Do not reintroduce direct Python DB driver hot paths, external dump tool paths, or the retired helper alias in `src/`, tests, packaging, or user-facing docs.
- Export/import uses `src/exporters/rust_dump_exporter.py` and the Rust JSONL commands `dump.run` / `dump.import`.
- Cross-engine migration uses the Rust core service for inspect, preflight, plan, migrate, verify, and resume.
- Packaging should include the single Rust DB core binary `tunnelforge-core(.exe)`. The previous helper alias has been removed from package/test expectations.
- Regression checks used for this transition: no legacy DB driver/tool/helper names in active code/docs, Rust tests/build pass, and full Python `pytest` passes.

## Commands

```bash
# Setup
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -e .              # 기본 의존성 설치
pip install -e ".[dev]"       # 개발 의존성 포함 (PyInstaller 등)

# Run application
python main.py

# Syntax check
python -m py_compile main.py
python -m py_compile src/core/*.py
python -m py_compile src/exporters/*.py
python -m py_compile src/ui/*.py
python -m py_compile src/ui/dialogs/*.py
python -m py_compile src/ui/workers/*.py

# Build (Windows)
.\scripts\build-installer.ps1           # Windows Installer 빌드 (오프라인, ~35MB)
.\scripts\build-installer.ps1 -Clean    # 이전 빌드 정리 후 빌드
.\scripts\build-bootstrapper.ps1        # 부트스트래퍼 빌드 (온라인, ~5MB)
.\scripts\build-bootstrapper.ps1 -Clean # 이전 빌드 정리 후 빌드

# Version Management & Release
# 🚀 Smart Release (권장) - GitHub와 비교하여 자동 버전 관리
/release                                    # Claude Code에서 스마트 릴리스
python scripts/smart_release.py             # Python 직접 실행 (권장)
python scripts/smart_release.py --dry-run   # 미리보기
./scripts/smart-release.sh                  # Bash 버전 (Python 없을 때)
./scripts/smart-release.sh --dry-run        # Bash 미리보기

# Legacy - PowerShell 버전 (인코딩 문제 가능성 있음)
.\scripts\smart-release.ps1                 # PowerShell (UTF-8 BOM 필요)
.\scripts\bump-version.ps1 -Type patch -AutoRelease   # 수동 타입 지정
```

## Architecture

```
main.py (Entry Point)
├── src/core/
│   ├── ConfigManager (config_manager.py)
│   │   └── Stores tunnel configs in %APPDATA%\Local\TunnelForge\config.json
│   ├── TunnelEngine (tunnel_engine.py)
│   │   ├── SSHTunnelForwarder for SSH tunnel mode
│   │   └── Direct connection mode support
│   ├── DbCoreFacade (db_core_service.py) - Rust DB core JSONL facade
│   └── DB connector shims - compatibility wrappers over Rust core
├── src/exporters/
│   └── RustDumpExporter (rust_dump_exporter.py) - dump/import via Rust core
└── src/ui/
    ├── TunnelManagerUI (main_window.py)
    ├── dialogs/
    │   ├── tunnel_config.py - Tunnel config dialog
    │   ├── settings.py - Settings, close confirm dialogs
    │   └── db_dialogs.py - DB connection, export/import wizards
    └── workers/
        └── rust_dump_worker.py - QThread worker for Rust dump/import operations
```

### Key Components

- **TunnelEngine** (`src/core/tunnel_engine.py`): Manages SSH tunnel lifecycle. Supports RSA, Ed25519, ECDSA keys via Paramiko. Two modes: SSH tunnel through bastion or direct connection.

- **DbCoreFacade** (`src/core/db_core_service.py`): Long-lived JSONL client for `tunnelforge-core`. DB credentials, schema calls, SQL execution, dump/import, and migration commands go through this facade.

- **RustDumpExporter** (`src/exporters/rust_dump_exporter.py`): Export/import wrapper over Rust `dump.run` and `dump.import`. Partial exports auto-include FK parent tables via `RustDumpExporter._resolve_required_tables_from_rust_schema` (Rust-schema driven). `ForeignKeyResolver` in the same module is used for orphan-record dependency analysis, not for export table selection.

- **UI Threading**: Long operations run in `QThread` workers such as `src/ui/workers/rust_dump_worker.py` and `src/ui/workers/cross_engine_migration_worker.py` to keep UI responsive.

### Connection Flow

1. User configures tunnel (bastion host, SSH key, target DB)
2. TunnelEngine establishes SSHTunnelForwarder
3. PyQt opens a Rust DB core facade session against the tunnel's local port
4. Export/import, schema, SQL, and migration flows call `tunnelforge-core`

## Project Structure

```
tunnel-manager/
├── main.py                     # Entry point
├── src/
│   ├── __init__.py
│   ├── core/                   # Core business logic
│   │   ├── __init__.py
│   │   ├── config_manager.py
│   │   ├── tunnel_engine.py
│   │   ├── db_core_service.py
│   │   └── db_connector.py
│   ├── exporters/              # DB Export/Import
│   │   ├── __init__.py
│   │   └── rust_dump_exporter.py
│   └── ui/                     # PyQt6 UI
│       ├── __init__.py
│       ├── main_window.py
│       ├── dialogs/
│       │   ├── __init__.py
│       │   ├── tunnel_config.py
│       │   ├── settings.py
│       │   └── db_dialogs.py
│       └── workers/
│           ├── __init__.py
│           └── rust_dump_worker.py
├── bootstrapper/               # Online installer (bootstrapper)
│   ├── __init__.py
│   ├── version_info.py         # Bootstrapper version & GitHub info
│   ├── downloader.py           # GitHub release download logic
│   ├── bootstrapper.py         # tkinter GUI main
│   └── bootstrapper.spec       # PyInstaller build config
├── assets/                     # Resource files
│   ├── icon.ico
│   ├── icon.png
│   ├── icon.svg
│   └── icon_512.png
├── pyproject.toml              # Package settings and dependencies
└── README.md
```

## Code Conventions

- Korean comments for UI text and functionality descriptions
- Emoji prefixes for status messages (✅, ❌, 🔗, 🚀)
- Return tuples `(success: bool, message: str)` for operation results
- Context manager pattern for database connections
- Imports use absolute paths from project root (e.g., `from src.core import ConfigManager`)

## Version Management & Release Process

### Version Management

- **Single Source of Truth**: `src/version.py`
- All version references (installer, app UI, GitHub releases) sync from this file
- Version format: Semantic Versioning (e.g., "1.0.0", "1.2.3")

### Release Workflow

#### PR 라벨 기반 자동 릴리스 (기본 방식)

PR에 라벨을 붙이면 머지 시 자동으로 릴리스가 진행됩니다.

| 라벨 | 버전 bump | 예시 |
|------|-----------|------|
| `version:patch` | 1.11.0 → 1.11.1 | 버그 수정 |
| `version:minor` | 1.11.0 → 1.12.0 | 새 기능 추가 |
| `version:major` | 1.11.0 → 2.0.0 | Breaking changes |

**자동화 흐름**:
1. Feature PR에 `version:*` 라벨 추가
2. `version-gate.yml` 실행 → PR 브랜치에 `chore: bump version to vX.Y.Z [patch]` 커밋 자동 push
3. PR 머지 → `create-release-tag.yml` 실행 → `src/version.py`에서 버전 읽어 태그 `vX.Y.Z` 생성
4. → `release.yml` 트리거 → GitHub Release + 인스톨러 빌드

**라벨 최초 설정** (저장소당 1회):
```bash
bash scripts/setup-labels.sh
```

**주의사항**:
- 복수 version 라벨 금지 (워크플로 에러 + PR 코멘트로 알림)
- GitHub App 설정 필요 (`RELEASER_APP_ID`, `RELEASER_APP_PRIVATE_KEY` 시크릿)
  - 필요 권한: `contents: write`, `pull-requests: write`

#### 긴급 Fallback (수동 릴리스)

자동 릴리스가 실패하거나 긴급 릴리스가 필요한 경우에만 사용:

```bash
# GitHub와 자동 비교하여 스마트하게 릴리스
/release

동작 방식:
1. GitHub API로 최신 릴리스 확인 (예: v1.2.3)
2. 로컬 src/version.py와 비교

시나리오 A: 버전 동일
→ 어떻게 올릴지 인터랙티브 선택 (patch/minor/major)
→ 자동 bump + commit + tag + push

시나리오 B: 로컬이 더 높음
→ 현재 버전으로 릴리스할지 확인
→ 태그만 생성 및 push

시나리오 C: 원격이 더 높음
→ 경고 메시지 출력 후 종료
```

### Scripts 구조

```
scripts/
├── versioning.py          # 📦 공유 버저닝 모듈 (bump_version.py, smart_release.py 공용)
├── bump_version.py        # ⚙️ GitHub Actions용 버전 bump CLI
├── setup-labels.sh        # 🏷️ GitHub 저장소 version 라벨 생성 (최초 1회)
├── smart_release.py       # 🚨 긴급 fallback용 수동 릴리스
├── smart-release.sh       # 🚨 긴급 fallback (Bash 버전)
├── build-installer.ps1    # ⚠️ GitHub Actions 전용 (삭제 금지!)
└── build-bootstrapper.ps1 # 부트스트래퍼(온라인 설치) 빌드
```

### Script 상세

- **`scripts/versioning.py`** - 📦 공유 버저닝 모듈
  - `read_version`, `write_version`, `sync_pyproject`, `bump_version`, `compare_versions`
  - `bump_version.py`와 `smart_release.py`에서 공유 사용

- **`scripts/bump_version.py`** - ⚙️ GitHub Actions용 버전 bump CLI
  - `--bump-type patch|minor|major` 로 버전 증가
  - `--dry-run` 옵션으로 파일 수정 없이 미리보기
  - stdout: `new_version=X.Y.Z` (`$GITHUB_OUTPUT` 호환)

- **`scripts/setup-labels.sh`** - 🏷️ GitHub 라벨 생성 (최초 1회)
  - `version:major`, `version:minor`, `version:patch` 라벨 생성
  - `--force` 옵션으로 멱등성 보장

- **`scripts/smart_release.py`** - 🚨 긴급 fallback (권장 아님)
  - 자동화 실패 시 수동 릴리스용
  - `/release` 스킬로 실행 가능
  - `--dry-run` 옵션으로 미리보기

- **`scripts/smart-release.sh`** - Bash 버전
  - Python이 없을 때 대체용

- **`scripts/build-installer.ps1`** - ⚠️ GitHub Actions 전용
  - `.github/workflows/release.yml`에서 사용
  - Windows Installer 빌드용 (PyInstaller + Inno Setup)
  - **로컬에서 사용하지 않음, 삭제 금지!**

### GitHub Actions

- **`.github/workflows/version-gate.yml`**: PR 라벨 기반 버전 bump
  - `version:*` 라벨 감지 시 PR 브랜치에 bump 커밋 자동 push
  - 복수 라벨 검증, 중복 bump 방지 (멱등성)
  - 브랜치 보호 규칙의 필수 상태 체크로 사용

- **`.github/workflows/create-release-tag.yml`**: PR 머지 시 태그 생성
  - `version:*` 라벨이 있는 PR 머지 시 실행
  - `src/version.py`에서 버전 읽어 `vX.Y.Z` 태그 생성
  - → `release.yml` 트리거

- **`.github/workflows/release.yml`**: 빌드 및 릴리스
  - `v*` 태그로 트리거 (e.g., v1.0.2)
  - `windows-latest` 러너에서 빌드
  - Inno Setup (Chocolatey로 설치) 사용
  - 오프라인 인스톨러 (~35MB) + 부트스트래퍼 (~5MB) 빌드
  - 릴리스 노트 자동 생성
  - GitHub Release에 업로드:
    - `TunnelForge-Setup-{version}.exe` - 오프라인 설치
    - `TunnelForge-WebSetup.exe` - 온라인 설치 (부트스트래퍼)

### Update Checker

- `src/core/update_checker.py`: GitHub Releases API integration
- Compares local version with latest GitHub release
- UI shows update notification in Settings → About tab
- Auto-check on app startup (configurable)
