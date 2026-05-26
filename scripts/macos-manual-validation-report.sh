#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

RUN_SMOKE=0
if [[ "${1:-}" == "--run-smoke" ]]; then
  RUN_SMOKE=1
fi

mkdir -p build
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_PATH="${MACOS_VALIDATION_REPORT:-build/macos-manual-validation-report-${TIMESTAMP}.md}"
SMOKE_LOG_PATH="${MACOS_VALIDATION_SMOKE_LOG:-build/macos-release-smoke-${TIMESTAMP}.log}"
mkdir -p "$(dirname "$REPORT_PATH")" "$(dirname "$SMOKE_LOG_PATH")"
VERSION="$(python -c 'from src.version import __version__; print(__version__)')"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
PYTHON_VERSION="$(python --version 2>&1 || echo unavailable)"
CARGO_VERSION="$(cargo --version 2>/dev/null || echo unavailable)"
MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null || echo unavailable)"
MACOS_BUILD="$(sw_vers -buildVersion 2>/dev/null || echo unavailable)"
ARCH="$(uname -m)"

SMOKE_STATUS="not run"
if [[ "$RUN_SMOKE" -eq 1 ]]; then
  set +e
  bash scripts/validate-macos-release.sh 2>&1 | tee "$SMOKE_LOG_PATH"
  smoke_exit="${PIPESTATUS[0]}"
  set -e

  if [[ "$smoke_exit" -eq 0 ]]; then
    SMOKE_STATUS="passed"
  else
    SMOKE_STATUS="failed (exit ${smoke_exit})"
  fi
fi

cat > "$REPORT_PATH" <<EOF
# TunnelForge macOS Manual Validation Report

## Metadata

- UTC time: ${TIMESTAMP}
- TunnelForge version: ${VERSION}
- Git SHA: ${GIT_SHA}
- macOS: ${MACOS_VERSION} (${MACOS_BUILD})
- Architecture: ${ARCH}
- Python: ${PYTHON_VERSION}
- Cargo: ${CARGO_VERSION}
- Release smoke: ${SMOKE_STATUS}
- Smoke log: ${SMOKE_LOG_PATH}

## Automated Smoke

- [ ] Run \`bash scripts/validate-macos-release.sh\`
- [ ] Confirm source \`python main.py --ui-smoke-check\` passed
- [ ] Confirm built app smoke passed
- [ ] Confirm mounted DMG smoke passed
- [ ] Confirm copied DMG install smoke passed
- [ ] Confirm ZIP extracted app smoke passed

## Interactive App Launch

- [ ] Launch \`python main.py\`
- [ ] Launch \`dist/TunnelForge.app\`
- [ ] Install from DMG into \`/Applications\` and launch \`/Applications/TunnelForge.app\`
- [ ] Confirm \`tunnelforge-core\` starts from inside the app

## SSH Tunnel

- [ ] Create an SSH tunnel
- [ ] Confirm tunnel monitoring updates
- [ ] Confirm reconnect behavior if applicable
- [ ] Close the SSH tunnel cleanly

## Database Connections

- [ ] Test MySQL connection through Rust DB Core
- [ ] Test PostgreSQL connection through Rust DB Core
- [ ] Test direct connection mode if applicable

## Export/Import

- [ ] Run Export/Import on a disposable MySQL database
- [ ] Run Export/Import on a disposable PostgreSQL database
- [ ] Confirm exported files and imported rows are correct

## Migration

- [ ] Run inspect
- [ ] Run preflight
- [ ] Run plan
- [ ] Run migrate
- [ ] Run verify
- [ ] Run resume after an interrupted disposable migration

## Settings And User Paths

- [ ] Confirm config files use macOS user directories
- [ ] Confirm logs use macOS user directories
- [ ] Confirm SQL history uses macOS user directories
- [ ] Confirm migration state, analysis, and rollback files use macOS user directories

## LaunchAgent

- [ ] Enable startup in settings
- [ ] Confirm \`~/Library/LaunchAgents/io.sanghyun.tunnelforge.plist\` exists
- [ ] Confirm LaunchAgent points to the expected app path
- [ ] Confirm LaunchAgent writes stdout to \`~/Library/Logs/TunnelForge/launchagent.out.log\`
- [ ] Confirm LaunchAgent writes stderr to \`~/Library/Logs/TunnelForge/launchagent.err.log\`
- [ ] Disable startup in settings
- [ ] Confirm LaunchAgent is removed

## Updates

- [ ] Confirm macOS update selection prefers the current architecture DMG
- [ ] Confirm the update UI opens the downloaded package
- [ ] Confirm the update UI does not execute DMG or ZIP as a program

## Signing, Notarization, And Gatekeeper

- [ ] Run \`codesign --verify --deep --strict --verbose=2 /Applications/TunnelForge.app\`
- [ ] Run \`spctl --assess --type execute --verbose /Applications/TunnelForge.app\`
- [ ] Confirm notarization status if distributing outside internal testing
- [ ] Confirm first launch behavior after download/install

## Result

- Overall result: pending
- Validator:
- Notes:

EOF

echo "Created $REPORT_PATH"
echo
echo "Fill every checkbox before closing the macOS manual validation gate."
