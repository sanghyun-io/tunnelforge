# TunnelDB Manager

A PyQt6 GUI application for managing SSH tunnels and MySQL database connections.

[한국어](README.ko.md)

## Features

- **SSH Tunnel Management**: Secure remote database access through bastion hosts
- **Direct Connection Mode**: Connect directly to local or external databases
- **MySQL Shell Export**: Fast schema/table export with parallel processing
- **MySQL Shell Import**: Parallel dump file import
- **Automatic GitHub Issue Reporting**: Auto-create GitHub issues on export/import errors
- **System Tray**: Background execution support

## Installation

### Requirements

- Python 3.9+
- MySQL Shell (required for Export/Import features)

### Setup

```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# Install dependencies
pip install -e .

# Install with development dependencies (PyInstaller, etc.)
pip install -e ".[dev]"
```

### GitHub Issue Auto-Reporting Setup (Optional)

To automatically create GitHub issues on export/import errors:

1. Copy `.env.example` to create a `.env` file:
   ```bash
   cp .env.example .env
   ```

2. Set up GitHub App (see [GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md) for details)

3. Place the Private Key in the `secrets/` directory:
   ```bash
   cp ~/Downloads/your-app.private-key.pem secrets/github-app-private-key.pem
   ```

4. Configure `.env` file:
   ```bash
   GITHUB_APP_ID=123456
   GITHUB_APP_PRIVATE_KEY=secrets/github-app-private-key.pem
   GITHUB_APP_INSTALLATION_ID=12345678
   GITHUB_REPO=your-org/your-repo
   ```

## Usage

```bash
python main.py
```

## Project Structure

```
tunnel-manager/
├── main.py                     # Entry point
├── src/
│   ├── __init__.py
│   ├── core/                   # Core business logic
│   │   ├── __init__.py
│   │   ├── config_manager.py       # Configuration management
│   │   ├── tunnel_engine.py        # SSH tunnel engine
│   │   ├── db_connector.py         # MySQL connection
│   │   ├── github_app_auth.py      # GitHub App authentication
│   │   └── github_issue_reporter.py # GitHub issue auto-reporting
│   ├── exporters/              # DB Export/Import
│   │   ├── __init__.py
│   │   └── mysqlsh_exporter.py # MySQL Shell based Export/Import
│   └── ui/                     # PyQt6 UI
│       ├── __init__.py
│       ├── main_window.py      # Main window
│       ├── dialogs/
│       │   ├── __init__.py
│       │   ├── tunnel_config.py    # Tunnel configuration dialog
│       │   ├── settings.py         # Settings dialog
│       │   └── db_dialogs.py       # DB Export/Import dialogs
│       └── workers/
│           ├── __init__.py
│           └── mysql_worker.py     # MySQL Shell worker thread
├── assets/                     # Resource files
│   ├── icon.ico
│   ├── icon.png
│   ├── icon.svg
│   └── icon_512.png
├── secrets/                    # GitHub App Private Key (Git ignored)
│   ├── README.md
│   └── github-app-private-key.pem.example
├── .env.example                # Environment variables template
├── pyproject.toml              # Package settings and dependencies
├── CLAUDE.md                   # Claude Code guide
├── GITHUB_APP_SETUP.md         # GitHub App setup guide
└── .gitignore
```

## Configuration File Location

- **Windows**: `%LOCALAPPDATA%\TunnelDB\config.json`
- **Linux/macOS**: `~/.config/tunneldb/config.json`

## Development & Build

### Building Windows Installer

To build the Windows Installer locally:

```bash
# Install dependencies (including dev tools)
pip install -e ".[dev]"

# Build installer
.\scripts\build-installer.ps1

# Clean build files before building
.\scripts\build-installer.ps1 -Clean
```

**Requirements:**
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) must be installed

Build outputs:
- `dist\TunnelDBManager.exe` - Executable
- `output\TunnelDBManager-Setup-{version}.exe` - Windows Installer

### Release Process

This project uses **automated build and release via GitHub Actions**.

#### Version Management Principles

- **Single Source of Truth**: Only manage `__version__` in `src/version.py`
- Pushing a Git tag automatically triggers build and release

#### How to Release a New Version

**Method 1: Automatic Version Bump (Recommended)**

<table>
<tr>
<td width="50%"><b>PowerShell / CMD</b></td>
<td width="50%"><b>Git Bash / Linux / macOS</b></td>
</tr>
<tr>
<td>

```powershell
# Patch version bump + auto release
.\scripts\bump-version.ps1 -Type patch -AutoRelease

# Minor version bump + auto release
.\scripts\bump-version.ps1 -Type minor -AutoRelease

# Major version bump + auto release
.\scripts\bump-version.ps1 -Type major -AutoRelease
```

</td>
<td>

```bash
# Patch version bump + auto release
./scripts/bump-version -Type patch -AutoRelease

# Minor version bump + auto release
./scripts/bump-version -Type minor -AutoRelease

# Major version bump + auto release
./scripts/bump-version -Type major -AutoRelease
```

</td>
</tr>
</table>

**This single command:**
- Auto-increments version
- Updates files
- Commits & pushes
- Creates & pushes tag
- Triggers GitHub Actions

**Method 2: Manual Version Management**

```bash
# 1. Manually update version in src/version.py
# Change __version__ = "1.0.1" to "1.0.2"

# 2. Commit changes
git add .
git commit -m "Bump version to 1.0.2"
git push origin main

# 3. Run release script
# PowerShell / CMD
.\scripts\create-release.ps1

# Git Bash / Linux / macOS
./scripts/create-release
```

**Preview (DryRun)**

<table>
<tr>
<td width="50%"><b>PowerShell / CMD</b></td>
<td width="50%"><b>Git Bash / Linux / macOS</b></td>
</tr>
<tr>
<td>

```powershell
.\scripts\bump-version.ps1 -Type patch -DryRun
.\scripts\create-release.ps1 -DryRun
```

</td>
<td>

```bash
./scripts/bump-version -Type patch -DryRun
./scripts/create-release -DryRun
```

</td>
</tr>
</table>

**View Script Help**

<table>
<tr>
<td width="50%"><b>PowerShell / CMD</b></td>
<td width="50%"><b>Git Bash / Linux / macOS</b></td>
</tr>
<tr>
<td>

```powershell
# Quick help
.\scripts\bump-version.ps1 -Help
.\scripts\bump-version.ps1 -h

# Detailed help
Get-Help .\scripts\bump-version.ps1 -Detailed
```

</td>
<td>

```bash
# View help
./scripts/bump-version -h
./scripts/create-release -h
./scripts/build-installer -h
```

</td>
</tr>
</table>

#### Automation Process

When a `v*` tag is pushed, GitHub Actions automatically:

1. Verifies version consistency (`src/version.py` vs Git tag)
2. Builds EXE with PyInstaller
3. Creates Windows Installer with Inno Setup
4. Creates GitHub Release
5. Attaches Installer to Release

**Check build progress:**
- https://github.com/sanghyun-io/db-connector/actions

**Check releases:**
- https://github.com/sanghyun-io/db-connector/releases

#### Dry Run (Preview)

Preview before actual release:

```bash
.\scripts\create-release.ps1 -DryRun
```

## Claude Code Commands

This project provides Claude Code commands to simplify the release process.

### Available Commands

#### `/release` - Auto Release

One-click release from version bump to GitHub Actions trigger:

```
/release
```

Interactive steps:
1. Check current version
2. Select version type (patch/minor/major)
3. Preview with DryRun
4. Execute auto release

#### `/bump` - Version Bump Only

Only increment version in src/version.py (for manual commit):

```
/bump
```

Interactive steps:
1. Check current version
2. Select version type
3. Update files only
4. Git operations done manually

#### `/release-guide` - Release Guide

View the complete release process guide:

```
/release-guide
```

Includes:
- Version management principles
- Release workflow
- GitHub Actions automation
- Troubleshooting

### Command Comparison

| Command | Version Bump | Git Operations | Release | When to Use |
|---------|-------------|----------------|---------|-------------|
| `/release` | Yes | Auto | Yes | One-click release (recommended) |
| `/bump` | Yes | Manual | No | Bump version only, commit later |
| `/release-guide` | No | No | No | View guide documentation |

## License

This project is distributed under the MIT License. See the [LICENSE](LICENSE) file for details.
