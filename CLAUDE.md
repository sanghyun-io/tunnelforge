# Language
**Allways Answer Korean**

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TunnelDB Manager - Python PyQt6 GUI application for managing SSH tunnels and MySQL database connections. Enables secure remote database access through SSH bastion hosts with database export functionality.

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
.\scripts\build-installer.ps1           # Windows Installer 빌드
.\scripts\build-installer.ps1 -Clean    # 이전 빌드 정리 후 빌드

# Version Management & Release
# 🚀 Smart Release (권장) - GitHub와 비교하여 자동 버전 관리
/release                                               # GitHub 버전과 비교하여 스마트 릴리스
.\scripts\smart-release.ps1                            # PowerShell 직접 실행
.\scripts\smart-release.ps1 -DryRun                    # 미리보기

# Legacy - 수동 타입 지정 방식 (기존 워크플로우)
.\scripts\bump-version.ps1 -Type patch -AutoRelease   # 패치 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type minor -AutoRelease   # 마이너 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type major -AutoRelease   # 메이저 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type patch -DryRun        # 미리보기

# Release (수동 버전 관리 시 - 태그만 생성)
.\scripts\create-release.ps1                           # PowerShell
.\scripts\create-release.ps1 -DryRun                   # 미리보기
```

## Architecture

```
main.py (Entry Point)
├── src/core/
│   ├── ConfigManager (config_manager.py)
│   │   └── Stores tunnel configs in %APPDATA%\Local\TunnelDB\config.json
│   ├── TunnelEngine (tunnel_engine.py)
│   │   ├── SSHTunnelForwarder for SSH tunnel mode
│   │   └── Direct connection mode support
│   └── MySQLConnector (db_connector.py) - PyMySQL wrapper
├── src/exporters/
│   └── MySQLShellExporter (mysqlsh_exporter.py) - Parallel export via mysqlsh
└── src/ui/
    ├── TunnelManagerUI (main_window.py)
    ├── dialogs/
    │   ├── tunnel_config.py - Tunnel config dialog
    │   ├── settings.py - Settings, close confirm dialogs
    │   └── db_dialogs.py - DB connection, export/import wizards
    └── workers/
        └── mysql_worker.py - QThread worker for mysqlsh operations
```

### Key Components

- **TunnelEngine** (`src/core/tunnel_engine.py`): Manages SSH tunnel lifecycle. Supports RSA, Ed25519, ECDSA keys via Paramiko. Two modes: SSH tunnel through bastion or direct connection.

- **MySQLShellExporter** (`src/exporters/mysqlsh_exporter.py`): Parallel export/import using MySQL Shell CLI. `ForeignKeyResolver` auto-includes parent tables for partial exports.

- **UI Threading**: Long operations (exports) run in `QThread` (`src/ui/workers/mysql_worker.py`) to keep UI responsive. Worker classes emit signals for progress updates.

### Connection Flow

1. User configures tunnel (bastion host, SSH key, target DB)
2. TunnelEngine establishes SSHTunnelForwarder
3. MySQLConnector connects via tunnel's local port
4. Export wizards use mysqlsh for parallel processing

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
│   │   └── db_connector.py
│   ├── exporters/              # DB Export/Import
│   │   ├── __init__.py
│   │   └── mysqlsh_exporter.py
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
│           └── mysql_worker.py
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

**🚀 Smart Release (권장):**

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

장점:
✅ GitHub 버전 자동 확인
✅ 실수 방지 (중복 릴리스, 버전 충돌)
✅ 상황에 맞는 액션 제안
✅ UX/DX 최적화

GitHub Actions (automatic):
- Verifies version consistency
- Builds Windows EXE (PyInstaller)
- Builds Windows Installer (Inno Setup)
- Creates GitHub Release
- Attaches installer to release
```

**레거시 - 수동 타입 지정 방식:**

```bash
# 사용자가 직접 bump 타입 결정 (GitHub 확인 없음)
.\scripts\bump-version.ps1 -Type patch -AutoRelease

This does:
1. Reads current version from src/version.py
2. Increments version (patch/minor/major) - 사용자 지정
3. Updates src/version.py
4. Commits changes
5. Pushes to GitHub (main branch)
6. Creates and pushes Git tag
7. Triggers GitHub Actions

단점:
❌ GitHub 버전 확인 안함
❌ 중복 릴리스 가능
❌ 수동 버전 관리 시 릴리스 누락 가능
```

**수동 워크플로우:**

```bash
1. Update src/version.py manually
   __version__ = "1.0.1"  →  "1.0.2"

2. Commit & push
   git add .
   git commit -m "Bump version to 1.0.2"
   git push origin main

3. Create release tag
   .\scripts\create-release.ps1

4. GitHub Actions runs automatically
```

### Build Scripts

- `scripts/smart-release.ps1`: **🚀 Smart Release (권장)**
  - GitHub API로 최신 릴리스 확인
  - 로컬 버전과 비교하여 적절한 액션 제안
  - 동일 버전: 인터랙티브 선택 (patch/minor/major)
  - 로컬이 높음: 릴리스 확인 후 태그만 생성
  - 원격이 높음: 경고 출력
  - `/release` 스킬로 실행 가능
  - Use `-DryRun` for preview

- `scripts/bump-version.ps1`: Version management (레거시)
  - Automatically increments version (major/minor/patch)
  - Updates `src/version.py`
  - Optional `-AutoRelease` for one-command release
  - GitHub 버전 확인 없음
  - Use `-DryRun` for preview

- `scripts/build-installer.ps1`: Local Windows Installer build
  - Syncs version from `src/version.py` to `installer/TunnelDBManager.iss`
  - Runs PyInstaller → Inno Setup

- `scripts/create-release.ps1`: Release tag creation (manual workflow)
  - Reads version, creates tag, pushes to GitHub
  - 버전 업데이트는 하지 않음 (태그만 생성)
  - Use `-DryRun` for preview without execution

### GitHub Actions

- `.github/workflows/release.yml`: Automated build & release
  - Triggered by `v*` tags (e.g., v1.0.2)
  - Builds on `windows-latest` runner
  - Installs Inno Setup via Chocolatey
  - Generates release notes automatically
  - Uploads installer to GitHub Release

### Update Checker

- `src/core/update_checker.py`: GitHub Releases API integration
- Compares local version with latest GitHub release
- UI shows update notification in Settings → About tab
- Auto-check on app startup (configurable)
