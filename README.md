# TunnelDB Manager

Secure database management through SSH tunnels with a simple GUI.

[한국어](README.ko.md)

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## What is TunnelDB Manager?

TunnelDB Manager is a desktop application that simplifies secure database access through SSH tunnels. Connect to remote MySQL databases safely via bastion hosts without complex command-line configurations.

### Key Features

- **One-Click SSH Tunnel** - Connect to remote databases through bastion hosts with saved configurations
- **Direct Connection** - Also supports direct database connections for local or accessible databases
- **Fast Export/Import** - Leverage MySQL Shell's parallel processing for quick data transfers
- **System Tray** - Runs quietly in background, always ready when you need it

## Download

**[Download Latest Version](https://github.com/sanghyun-io/db-connector/releases/latest/download/TunnelDBManager-Setup-latest.exe)**

Or browse all versions at [Releases](https://github.com/sanghyun-io/db-connector/releases).

## Quick Start

### 1. Install

Run the downloaded installer and follow the setup wizard.

### 2. Add a Tunnel

1. Click **"Add Tunnel"** button
2. Enter connection details:
   - **Tunnel Name**: A friendly name (e.g., "Production DB")
   - **Bastion Host**: Your SSH jump server address
   - **SSH Key**: Path to your private key file
   - **Database Host**: Target database server (as seen from bastion)
   - **Database Credentials**: Username and password

3. Click **Save**

### 3. Connect

1. Select your tunnel from the list
2. Click **"Connect"**
3. Once connected, use the database tools:
   - **Export** - Backup schemas or tables
   - **Import** - Restore from backup files

## Tips

### Managing Multiple Environments

Create separate tunnel configurations for each environment (Development, Staging, Production) with clear naming conventions.

### Export Best Practices

- Use **schema-only export** for structure backups
- Use **table selection** to export only what you need
- Exports run in parallel for faster completion

### System Tray

- Minimize to tray to keep tunnels running in background
- Double-click tray icon to restore window
- Right-click for quick actions

## Requirements

- Windows 10 or later
- [MySQL Shell](https://dev.mysql.com/downloads/shell/) (for Export/Import features)

## Configuration

Settings are stored in: `%LOCALAPPDATA%\TunnelDB\config.json`

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT License - see [LICENSE](LICENSE) for details.
