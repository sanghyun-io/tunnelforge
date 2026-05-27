<div align="center">

<img src="assets/icon_512.png" width="128" alt="TunnelForge Logo" />

# TunnelForge

**Secure database management through SSH tunnels — no CLI required.**

[한국어](README.ko.md) · [English](README.md)

[![GitHub Release](https://img.shields.io/github/v/release/sanghyun-io/tunnelforge?style=flat-square&logo=github&label=Release)](https://github.com/sanghyun-io/tunnelforge/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/sanghyun-io/tunnelforge/total?style=flat-square&logo=github&label=Downloads)](https://github.com/sanghyun-io/tunnelforge/releases)
[![Build](https://img.shields.io/github/actions/workflow/status/sanghyun-io/tunnelforge/release.yml?style=flat-square&logo=githubactions&logoColor=white&label=Build)](https://github.com/sanghyun-io/tunnelforge/actions)
[![License](https://img.shields.io/github/license/sanghyun-io/tunnelforge?style=flat-square&label=License)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-0078D6?style=flat-square)](https://github.com/sanghyun-io/tunnelforge/releases)

</div>

---

## Features

### Connection & Tunnel Management

| | Feature | Description |
|:-:|---------|-------------|
| 🔐 | **SSH Tunnel** | One-click secure connection via bastion hosts. RSA, Ed25519, ECDSA keys supported. |
| 🔗 | **Direct Connect** | Skip the tunnel — connect directly to local or accessible databases. |
| 📡 | **Tunnel Monitoring** | Real-time tunnel health monitoring with auto-reconnect on failure. |
| 🖥️ | **System Tray** | Runs quietly in the background, always one click away. |

### Schema Management

| | Feature | Description |
|:-:|---------|-------------|
| 🔍 | **Schema Diff** | Visual side-by-side schema comparison between any two databases. |
| 🔄 | **Schema Sync** | Generate and execute sync scripts to align schemas across environments. |
| 🎨 | **Pixel Art Loading** | Fun pixel-art DB animation while comparing schemas. |

### Migration Tools

| | Feature | Description |
|:-:|---------|-------------|
| 🔄 | **Cross-Engine Migration** | Guided MySQL ↔ PostgreSQL migration powered by Rust DB Core. |
| 🛡️ | **Migration Analysis** | Compatibility checks help surface MySQL upgrade risks before changes are applied. |
| 💡 | **Fix Guidance** | Review suggested fixes for detected compatibility issues before applying changes. |
| 📊 | **Migration Report** | Export detailed HTML/JSON reports of compatibility findings. |

### Data Tools

| | Feature | Description |
|:-:|---------|-------------|
| ⚡ | **Parallel Export/Import** | Blazing-fast data transfers powered by Rust DB Core's parallel processing. |
| 📝 | **SQL Editor** | Built-in SQL editor with syntax highlighting and query history. |
| 🛡️ | **Production Guard** | Safety prompts and confirmations for production database operations. |

### General

| | Feature | Description |
|:-:|---------|-------------|
| 🔄 | **Auto Update** | Checks for new versions on startup so you never miss an update. |
| 🐛 | **Optional Issue Reporter** | GitHub App-based error reporting when explicitly configured. |

---

## Download

<div align="center">

[![Web Installer](https://img.shields.io/badge/⬇_Web_Installer-Recommended_(~5MB)-2563EB?style=for-the-badge)](https://github.com/sanghyun-io/tunnelforge/releases/latest/download/TunnelForge-WebSetup.exe)
&nbsp;&nbsp;
[![Offline Installer](https://img.shields.io/badge/⬇_Offline_Installer-Full_Package_(~35MB)-6B7280?style=for-the-badge)](https://github.com/sanghyun-io/tunnelforge/releases/latest/download/TunnelForge-Setup-latest.exe)

[Browse all releases for macOS DMG/ZIP →](https://github.com/sanghyun-io/tunnelforge/releases)

macOS DMG/ZIP packages are beta artifacts pending final real-Mac operator validation. They may have issues in SSH, DB, migration, LaunchAgent, or Gatekeeper flows; use them at your own risk and without warranty until final validation is complete.

</div>

---

## Quick Start

### 1. Install

Run the downloaded installer and follow the setup wizard. On macOS, download the DMG for your Mac architecture (`arm64` for Apple Silicon, `x86_64` for Intel), optionally verify it with the matching `.sha256` file, open it, and move `TunnelForge.app` to Applications.

### 2. Add a Tunnel

Click **"Add Tunnel"** and configure your connection:

| Field | Description | Example |
|-------|-------------|---------|
| Tunnel Name | A friendly label | `Production DB` |
| Bastion Host | SSH jump server | `bastion.example.com` |
| SSH Key | Private key file path | `C:\Users\me\.ssh\id_rsa` |
| DB Host | Target database (from bastion's perspective) | `db.internal:3306` |
| DB Credentials | Username & password | `admin` / `••••` |

### 3. Connect & Go

Select a tunnel → Click **"Connect"** → Use the database tools:
- **Export** — Backup schemas or selected tables
- **Import** — Restore from backup files

---

## How It Works

```mermaid
graph LR
    A["🖥️ TunnelForge"] -->|SSH Tunnel| B["🔒 Bastion Host"]
    B -->|Internal Network| C["🗄️ MySQL Server"]
    A -->|"Export / Import"| D["📁 Local Files"]

    style A fill:#2563EB,color:#fff,stroke:none
    style B fill:#F97316,color:#fff,stroke:none
    style C fill:#10B981,color:#fff,stroke:none
    style D fill:#6B7280,color:#fff,stroke:none
```

---

## Tips

<details>
<summary><b>Managing Multiple Environments</b></summary>

Create separate tunnel configs for each environment (Dev, Staging, Production) with clear naming — keep things organized.

</details>

<details>
<summary><b>Export Best Practices</b></summary>

- Use **schema-only export** for structure backups
- Use **table selection** to export only what you need
- Exports run in parallel for faster completion

</details>

<details>
<summary><b>System Tray Usage</b></summary>

- Minimize to tray to keep tunnels alive in the background
- Double-click the tray icon to restore the window
- Right-click for quick-action menu

</details>

---

## Requirements

| Requirement | Note |
|-------------|------|
| **Windows 10+** | Supported packaged platform |
| **macOS 13+** | Supported as a packaged app build; final device validation is required per release |
| **Rust DB Core binary** | Built and packaged with TunnelForge for Export/Import features |

For the macOS support scope and final validation checklist, see [macOS Support Plan](docs/macos_support.md).

## Configuration

Settings are stored at:

- Windows: `%LOCALAPPDATA%\TunnelForge\config.json`
- macOS: `~/Library/Application Support/TunnelForge/config.json`

---

<div align="center">

**[Contributing](CONTRIBUTING.md)** · **[License (MIT)](LICENSE)**

Made with ❤️ for database engineers who value security.

</div>
