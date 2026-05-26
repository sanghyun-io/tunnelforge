#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/macos-manual-validation-report.sh [--run-smoke]
  bash scripts/macos-manual-validation-report.sh --check-complete <report.md>

Options:
  --run-smoke                 Run scripts/validate-macos-release.sh while creating the report.
  --check-complete <report>   Verify a completed manual validation report has no open gates.
  --help                      Show this help.
EOF
}

RUN_SMOKE=0
CHECK_COMPLETE_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-smoke)
      RUN_SMOKE=1
      shift
      ;;
    --check-complete)
      if [[ -z "${2:-}" ]]; then
        echo "--check-complete requires a report path." >&2
        exit 2
      fi
      CHECK_COMPLETE_PATH="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

check_complete_report() {
  local report_path="$1"
  local failures=0

  if [[ ! -f "$report_path" ]]; then
    echo "Manual validation report not found: $report_path" >&2
    exit 1
  fi

  if grep -qE '^[[:space:]]*- \[ \]' "$report_path"; then
    echo "Manual validation report still has unchecked items:" >&2
    grep -nE '^[[:space:]]*- \[ \]' "$report_path" >&2
    failures=1
  fi

  if ! grep -qE '^- Release smoke: passed[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must record '- Release smoke: passed'." >&2
    failures=1
  fi

  if ! grep -qE '^- Overall result: (pass|passed|PASS|PASSED)[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must record '- Overall result: passed'." >&2
    failures=1
  fi

  if ! grep -qE '^- Validator:[[:space:]]*[^[:space:]].*$' "$report_path"; then
    echo "Manual validation report must include a validator name." >&2
    failures=1
  fi

  if [[ "$failures" -ne 0 ]]; then
    exit 1
  fi

  echo "Manual validation report is complete: $report_path"
}

if [[ -n "$CHECK_COMPLETE_PATH" ]]; then
  check_complete_report "$CHECK_COMPLETE_PATH"
  exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
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
FINAL_APP_PATH="${MACOS_VALIDATION_APP_PATH:-/Applications/TunnelForge.app}"
FINAL_APP_EXECUTABLE="${FINAL_APP_PATH}/Contents/MacOS/TunnelForge"

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
- Final app path: ${FINAL_APP_PATH}
- Final app executable: ${FINAL_APP_EXECUTABLE}
- Completion check: \`bash scripts/macos-manual-validation-report.sh --check-complete ${REPORT_PATH}\`

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
- [ ] Install from DMG into \`/Applications\` and launch \`${FINAL_APP_PATH}\`
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

- [ ] Run \`codesign --verify --deep --strict --verbose=2 ${FINAL_APP_PATH}\`
- [ ] Run \`spctl --assess --type execute --verbose ${FINAL_APP_PATH}\`
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
echo "Then run: bash scripts/macos-manual-validation-report.sh --check-complete $REPORT_PATH"
