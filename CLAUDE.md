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

### Scripts 구조

```
scripts/
├── smart_release.py       # 🚀 스마트 릴리스 (Python, 권장)
├── smart-release.sh       # 🚀 스마트 릴리스 (Bash, Python 없을 때)
├── build-installer.ps1    # ⚠️ GitHub Actions 전용 (삭제 금지!)
└── build-bootstrapper.ps1 # 부트스트래퍼(온라인 설치) 빌드
```

### Script 상세

- **`scripts/smart_release.py`** - 🚀 Smart Release (권장)
  - GitHub API로 최신 릴리스 확인
  - 로컬 버전과 비교하여 적절한 액션 제안
  - `/release` 스킬로 실행 가능
  - `--dry-run` 옵션으로 미리보기

- **`scripts/smart-release.sh`** - Bash 버전
  - Python이 없을 때 대체용
  - 동일한 기능 제공

- **`scripts/build-installer.ps1`** - ⚠️ GitHub Actions 전용
  - `.github/workflows/release.yml`에서 사용
  - Windows Installer 빌드용 (PyInstaller + Inno Setup)
  - **로컬에서 사용하지 않음, 삭제 금지!**

### GitHub Actions

- `.github/workflows/release.yml`: Automated build & release
  - Triggered by `v*` tags (e.g., v1.0.2)
  - Builds on `windows-latest` runner
  - Installs Inno Setup via Chocolatey
  - Builds offline installer (~35MB) and bootstrapper (~5MB)
  - Generates release notes automatically
  - Uploads all installers to GitHub Release:
    - `TunnelDBManager-Setup-{version}.exe` - 오프라인 설치
    - `TunnelDBManager-Setup-latest.exe` - 항상 최신 (오프라인)
    - `TunnelDBManager-WebSetup.exe` - 온라인 설치 (부트스트래퍼)

### Update Checker

- `src/core/update_checker.py`: GitHub Releases API integration
- Compares local version with latest GitHub release
- UI shows update notification in Settings → About tab
- Auto-check on app startup (configurable)
