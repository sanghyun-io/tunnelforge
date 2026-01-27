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
