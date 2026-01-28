# Contributing to TunnelForge

Thank you for your interest in contributing! This guide covers development setup, build process, and release workflow.

## Development Setup

### Requirements

- Python 3.9+
- Git
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) (for building Windows installer)

### Setup

```bash
# Clone repository
git clone https://github.com/sanghyun-io/tunnelforge.git
cd tunnelforge

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# Install with development dependencies
pip install -e ".[dev]"
```

### Running the Application

```bash
python main.py
```

## Project Structure

```
tunnel-manager/
├── main.py                 # Entry point
├── src/
│   ├── version.py          # Version (single source of truth)
│   ├── core/               # Business logic
│   │   ├── config_manager.py   # Configuration management
│   │   ├── tunnel_engine.py    # SSH tunnel engine
│   │   └── db_connector.py     # MySQL connection
│   ├── exporters/          # DB Export/Import
│   │   └── mysqlsh_exporter.py
│   └── ui/                 # PyQt6 UI
│       ├── main_window.py
│       ├── dialogs/
│       └── workers/
├── assets/                 # Icons and resources
├── installer/              # Inno Setup configuration
└── scripts/                # Build and release scripts
```

## Building

### Build Windows Installer Locally

```bash
# Build installer
.\scripts\build-installer.ps1

# Clean build
.\scripts\build-installer.ps1 -Clean
```

**Output:**
- `dist\TunnelForge.exe` - Standalone executable
- `output\TunnelForge-Setup-{version}.exe` - Windows installer

## Release Process

This project uses GitHub Actions for automated releases.

### Version Management

- **Single Source of Truth**: `src/version.py`
- Semantic Versioning: `major.minor.patch`

### Creating a Release

**Recommended: Smart Release**

```bash
# Claude Code command
/release

# Or PowerShell script
.\scripts\smart-release.ps1
```

This compares local version with GitHub releases and guides you through the process.

**Alternative: Manual Version Bump**

```powershell
# Bump version and release
.\scripts\bump-version.ps1 -Type patch -AutoRelease

# Preview without executing
.\scripts\bump-version.ps1 -Type patch -DryRun
```

### What Happens on Release

When a `v*` tag is pushed, GitHub Actions:

1. Verifies version consistency
2. Builds EXE with PyInstaller
3. Creates Windows Installer with Inno Setup
4. Creates GitHub Release
5. Attaches installer to release

**Monitor builds:** https://github.com/sanghyun-io/tunnelforge/actions

## Code Style

- Use meaningful variable and function names
- Add comments for complex logic (Korean comments are OK for UI text)
- Follow existing patterns in the codebase

## Submitting Changes

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## Questions?

Open an issue for questions or suggestions.
