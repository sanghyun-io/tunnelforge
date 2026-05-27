#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/macos-manual-validation-report.sh [--run-smoke]
  bash scripts/macos-manual-validation-report.sh --check-complete <report.md>
  bash scripts/macos-manual-validation-report.sh --bundle-evidence <report.md>
  bash scripts/macos-manual-validation-report.sh --finalize <report.md>

Options:
  --run-smoke                 Run scripts/validate-macos-release.sh while creating the report.
                              Set MACOS_RELEASE_SMOKE_APPLICATIONS=1 to include /Applications install smoke.
  --download-artifacts        Download and checksum-verify GitHub Actions macOS artifacts before report creation.
  --artifact-run-id <id>      Download artifacts from a specific macOS App Validation workflow run.
                              Defaults to the latest successful manual run for PR #117 head.
  --artifact-output-dir <dir> Destination for downloaded artifacts.
  --artifact-arch <arch>      Artifact architecture to download: arm64, x86_64, or all. Default: current Mac arch.
  --check-complete <report>   Verify a completed manual validation report has no open gates.
  --bundle-evidence <report>  Verify the completed report and create an attachable evidence zip.
  --finalize <report>         Verify, bundle, run the final gate, and print attachment paths.
  --post-github-comment       After --finalize, post the generated evidence comment to #116 and PR #117.
  --evidence-bundle <zip>     Override the evidence zip output path.
  --skip-github               Pass --skip-github to the final Python gate when finalizing offline.
  --help                      Show this help.
EOF
}

RUN_SMOKE=0
DOWNLOAD_ARTIFACTS=0
ARTIFACT_RUN_ID_ARG=""
ARTIFACT_OUTPUT_DIR_ARG=""
ARTIFACT_ARCH_ARG=""
CHECK_COMPLETE_PATH=""
BUNDLE_EVIDENCE_PATH=""
BUNDLE_OUTPUT_PATH=""
FINALIZE_PATH=""
SKIP_GITHUB=0
POST_GITHUB_COMMENT=0
PYTHON_BIN="${PYTHON:-}"
GH_BIN="${GH:-gh}"

if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    PYTHON_BIN="python3"
  fi
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-smoke)
      RUN_SMOKE=1
      shift
      ;;
    --download-artifacts)
      DOWNLOAD_ARTIFACTS=1
      shift
      ;;
    --artifact-run-id)
      if [[ -z "${2:-}" ]]; then
        echo "--artifact-run-id requires a run id." >&2
        exit 2
      fi
      ARTIFACT_RUN_ID_ARG="$2"
      shift 2
      ;;
    --artifact-output-dir)
      if [[ -z "${2:-}" ]]; then
        echo "--artifact-output-dir requires a directory." >&2
        exit 2
      fi
      ARTIFACT_OUTPUT_DIR_ARG="$2"
      shift 2
      ;;
    --artifact-arch)
      if [[ -z "${2:-}" ]]; then
        echo "--artifact-arch requires arm64, x86_64, or all." >&2
        exit 2
      fi
      ARTIFACT_ARCH_ARG="$2"
      shift 2
      ;;
    --check-complete)
      if [[ -z "${2:-}" ]]; then
        echo "--check-complete requires a report path." >&2
        exit 2
      fi
      CHECK_COMPLETE_PATH="$2"
      shift 2
      ;;
    --bundle-evidence)
      if [[ -z "${2:-}" ]]; then
        echo "--bundle-evidence requires a report path." >&2
        exit 2
      fi
      BUNDLE_EVIDENCE_PATH="$2"
      shift 2
      ;;
    --finalize)
      if [[ -z "${2:-}" ]]; then
        echo "--finalize requires a report path." >&2
        exit 2
      fi
      FINALIZE_PATH="$2"
      shift 2
      ;;
    --evidence-bundle)
      if [[ -z "${2:-}" ]]; then
        echo "--evidence-bundle requires an output zip path." >&2
        exit 2
      fi
      BUNDLE_OUTPUT_PATH="$2"
      shift 2
      ;;
    --skip-github)
      SKIP_GITHUB=1
      shift
      ;;
    --post-github-comment)
      POST_GITHUB_COMMENT=1
      shift
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

extract_smoke_log_path() {
  local report_path="$1"
  grep -m1 -E '^- Smoke log:' "$report_path" 2>/dev/null \
    | sed -E 's/^- Smoke log:[[:space:]]*//' \
    | tr -d '\r' \
    || true
}

extract_system_evidence_log_path() {
  local report_path="$1"
  grep -m1 -E '^- System evidence log:' "$report_path" 2>/dev/null \
    | sed -E 's/^- System evidence log:[[:space:]]*//' \
    | tr -d '\r' \
    || true
}

extract_report_value() {
  local report_path="$1"
  local key="$2"
  grep -m1 -E "^${key}:" "$report_path" 2>/dev/null \
    | sed -E "s/^${key}:[[:space:]]*//" \
    | tr -d '\r' \
    || true
}

sha256_file() {
  local path="$1"
  "$PYTHON_BIN" - "$path" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

read_artifact_env_value() {
  local env_path="$1"
  local key="$2"
  grep -m1 -E "^${key}=" "$env_path" \
    | sed -E "s/^${key}=//" \
    | tr -d '\r'
}

bundle_path_for_report() {
  local report_path="$1"
  local report_name=""

  report_name="$(basename "$report_path" .md)"
  echo "${BUNDLE_OUTPUT_PATH:-${MACOS_VALIDATION_EVIDENCE_BUNDLE:-build/macos-manual-validation-evidence-${report_name}.zip}}"
}

checksum_path_for_bundle() {
  local bundle_path="$1"
  echo "${bundle_path}.sha256"
}

comment_path_for_report() {
  local report_path="$1"
  local report_name=""

  report_name="$(basename "$report_path" .md)"
  echo "$(dirname "$report_path")/macos-final-validation-github-comment-${report_name}.md"
}

check_complete_report() {
  local report_path="$1"
  local failures=0
  local smoke_log_path=""
  local system_evidence_log_path=""

  if [[ ! -f "$report_path" ]]; then
    echo "Manual validation report not found: $report_path" >&2
    exit 1
  fi

  local required_sections=(
    "## Automated Smoke"
    "## Interactive App Launch"
    "## SSH Tunnel"
    "## Database Connections"
    "## Export/Import"
    "## Migration"
    "## Settings And User Paths"
    "## LaunchAgent"
    "## Updates"
    "## Signing, Notarization, And Gatekeeper"
    "## Result"
  )

  for required_section in "${required_sections[@]}"; do
    if ! grep -qF "$required_section" "$report_path"; then
      echo "Manual validation report is missing required section: $required_section" >&2
      failures=1
    fi
  done

  local required_check_items=(
    'Run `bash scripts/validate-macos-release.sh`'
    'Confirm source `python main.py --ui-smoke-check` passed'
    'Download signed/notarized GitHub Actions macOS artifacts'
    'Confirm downloaded macOS artifact checksums passed'
    'Confirm built app smoke passed'
    'Confirm mounted DMG smoke passed'
    'Confirm copied DMG install smoke passed'
    'Confirm ZIP extracted app smoke passed'
    'Launch `python main.py`'
    'Launch `dist/TunnelForge.app`'
    'Install from DMG into `/Applications` and launch'
    'Confirm `tunnelforge-core` starts from inside the app'
    'Create an SSH tunnel'
    'Confirm tunnel monitoring updates'
    'Close the SSH tunnel cleanly'
    'Test MySQL connection through Rust DB Core'
    'Test PostgreSQL connection through Rust DB Core'
    'Run Export/Import on a disposable MySQL database'
    'Run Export/Import on a disposable PostgreSQL database'
    'Confirm exported files and imported rows are correct'
    'Run inspect'
    'Run preflight'
    'Run plan'
    'Run migrate'
    'Run verify'
    'Run resume after an interrupted disposable migration'
    'Confirm config files use macOS user directories'
    'Confirm logs use macOS user directories'
    'Confirm SQL history uses macOS user directories'
    'Confirm migration state, analysis, and rollback files use macOS user directories'
    'Enable startup in settings'
    'Confirm `~/Library/LaunchAgents/io.sanghyun.tunnelforge.plist` exists'
    'Confirm LaunchAgent points to the expected app path'
    'Confirm LaunchAgent writes stdout to `~/Library/Logs/TunnelForge/launchagent.out.log`'
    'Confirm LaunchAgent writes stderr to `~/Library/Logs/TunnelForge/launchagent.err.log`'
    'Disable startup in settings'
    'Confirm LaunchAgent is removed'
    'Confirm macOS update selection prefers the current architecture DMG'
    'Confirm the update UI opens the downloaded package'
    'Confirm the update UI does not execute DMG or ZIP as a program'
    'Run `codesign --verify --deep --strict --verbose=2'
    'Run `spctl --assess --type execute --verbose'
    'Confirm notarization status if distributing outside internal testing'
    'Confirm first launch behavior after download/install'
  )

  for required_check_item in "${required_check_items[@]}"; do
    if ! grep -qF -- "- [x] $required_check_item" "$report_path"; then
      echo "Manual validation report is missing required checklist item: $required_check_item" >&2
      failures=1
    fi
  done

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

  if ! grep -qE '^- macOS:[[:space:]]*[0-9]+(\.[0-9]+){0,2}[[:space:]]+\([^)]+\)[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must include a real macOS version." >&2
    failures=1
  fi

  if ! grep -qE '^- Architecture:[[:space:]]*(arm64|x86_64)[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must include a supported Mac architecture." >&2
    failures=1
  fi

  if ! grep -qE '^- Artifact workflow run:[[:space:]]*[0-9]+[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must include a GitHub Actions artifact workflow run id." >&2
    failures=1
  fi

  if ! grep -qE '^- Artifact directory:[[:space:]]*[^[:space:]].*$' "$report_path"; then
    echo "Manual validation report must include a downloaded artifact directory." >&2
    failures=1
  fi

  if ! grep -qE '^- Artifact checksum verification:[[:space:]]*(pass|passed|PASS|PASSED)[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must record '- Artifact checksum verification: passed'." >&2
    failures=1
  fi

  if ! grep -qE '^- Final app path:[[:space:]]*/Applications/TunnelForge\.app[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must record final app path under /Applications." >&2
    failures=1
  fi

  if ! grep -qE '^- Final app executable:[[:space:]]*/Applications/TunnelForge\.app/Contents/MacOS/TunnelForge[[:space:]]*$' "$report_path"; then
    echo "Manual validation report must record final app executable under /Applications." >&2
    failures=1
  fi

  smoke_log_path="$(extract_smoke_log_path "$report_path")"

  if [[ -z "$smoke_log_path" ]]; then
    echo "Manual validation report must include a smoke log path." >&2
    failures=1
  elif [[ ! -s "$smoke_log_path" ]]; then
    echo "Smoke log is missing or empty: $smoke_log_path" >&2
    failures=1
  elif ! grep -q "macOS release package smoke checks passed." "$smoke_log_path"; then
    echo "Smoke log must include the successful release smoke completion message." >&2
    failures=1
  elif ! grep -q "macOS /Applications install smoke checks passed." "$smoke_log_path"; then
    echo "Smoke log must include the successful /Applications install smoke completion message." >&2
    failures=1
  fi

  system_evidence_log_path="$(extract_system_evidence_log_path "$report_path")"

  if [[ -z "$system_evidence_log_path" ]]; then
    echo "Manual validation report must include a system evidence log path." >&2
    failures=1
  elif [[ ! -s "$system_evidence_log_path" ]]; then
    echo "System evidence log is missing or empty: $system_evidence_log_path" >&2
    failures=1
  else
    if ! grep -q "== sw_vers ==" "$system_evidence_log_path" \
      || ! grep -q "ProductVersion:" "$system_evidence_log_path" \
      || ! grep -q "== uname ==" "$system_evidence_log_path" \
      || ! grep -q "Darwin" "$system_evidence_log_path" \
      || ! grep -q "== architecture ==" "$system_evidence_log_path" \
      || ! grep -q "== final app ==" "$system_evidence_log_path" \
      || ! grep -q "/Applications/TunnelForge.app" "$system_evidence_log_path" \
      || ! grep -q "== codesign verify ==" "$system_evidence_log_path" \
      || ! grep -q "== spctl assess ==" "$system_evidence_log_path"; then
      echo "System evidence log must include sw_vers, uname, architecture, final app, codesign, and spctl evidence." >&2
      failures=1
    fi

    if [[ "$(grep -cE '^exit: 0[[:space:]]*$' "$system_evidence_log_path")" -lt 2 ]]; then
      echo "System evidence log must show successful codesign and spctl checks." >&2
      failures=1
    fi
  fi

  if [[ "$failures" -ne 0 ]]; then
    exit 1
  fi

  echo "Manual validation report is complete: $report_path"
}

create_evidence_bundle() {
  local report_path="$1"
  local smoke_log_path=""
  local system_evidence_log_path=""
  local bundle_path=""

  check_complete_report "$report_path"
  smoke_log_path="$(extract_smoke_log_path "$report_path")"
  system_evidence_log_path="$(extract_system_evidence_log_path "$report_path")"
  bundle_path="$(bundle_path_for_report "$report_path")"
  mkdir -p "$(dirname "$bundle_path")"

  "$PYTHON_BIN" - "$report_path" "$smoke_log_path" "$system_evidence_log_path" "$bundle_path" <<'PY'
import hashlib
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

report_path = Path(sys.argv[1])
smoke_log_path = Path(sys.argv[2])
system_evidence_log_path = Path(sys.argv[3])
bundle_path = Path(sys.argv[4])
manifest_name = f"macos-manual-validation-evidence-{report_path.stem}.sha256"

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

manifest = (
    f"{digest(report_path)}  {report_path.name}\n"
    f"{digest(smoke_log_path)}  {smoke_log_path.name}\n"
    f"{digest(system_evidence_log_path)}  {system_evidence_log_path.name}\n"
)

with ZipFile(bundle_path, "w", ZIP_DEFLATED) as archive:
    archive.write(report_path, report_path.name)
    archive.write(smoke_log_path, smoke_log_path.name)
    archive.write(system_evidence_log_path, system_evidence_log_path.name)
    archive.writestr(manifest_name, manifest)

bundle_checksum_path = Path(f"{bundle_path}.sha256")
bundle_checksum_path.write_text(f"{digest(bundle_path)}  {bundle_path.name}\n", encoding="utf-8")
PY

  echo "Created $bundle_path"
  echo "Created $(checksum_path_for_bundle "$bundle_path")"
}

finalize_evidence() {
  local report_path="$1"
  local smoke_log_path=""
  local system_evidence_log_path=""
  local bundle_path=""
  local checksum_path=""
  local comment_path=""
  local report_git_sha=""
  local artifact_workflow_run=""
  local bundle_sha256=""
  local gate_args=()

  create_evidence_bundle "$report_path"
  smoke_log_path="$(extract_smoke_log_path "$report_path")"
  system_evidence_log_path="$(extract_system_evidence_log_path "$report_path")"
  bundle_path="$(bundle_path_for_report "$report_path")"
  checksum_path="$(checksum_path_for_bundle "$bundle_path")"
  comment_path="$(comment_path_for_report "$report_path")"
  report_git_sha="$(extract_report_value "$report_path" "- Git SHA")"
  artifact_workflow_run="$(extract_report_value "$report_path" "- Artifact workflow run")"
  bundle_sha256="$(sha256_file "$bundle_path")"
  gate_args=(--final --report "$report_path" --bundle "$bundle_path")

  if [[ "$SKIP_GITHUB" -eq 1 ]]; then
    gate_args+=(--skip-github)
  fi

  "$PYTHON_BIN" scripts/check-macos-support-gate.py "${gate_args[@]}"

  cat > "$comment_path" <<EOF
Final macOS validation evidence for #116

Final gate passed for the attached real-Mac evidence. Attach these files to #116 and PR #117 before checking the final device validation box:

- Git SHA: \`${report_git_sha}\`
- Artifact workflow run: \`${artifact_workflow_run}\`
- Evidence bundle SHA256: \`${bundle_sha256}\`
- Report: ${report_path}
- Smoke log: ${smoke_log_path}
- System evidence log: ${system_evidence_log_path}
- Evidence bundle: ${bundle_path}
- Evidence bundle checksum: ${checksum_path}

Suggested GitHub comment command after attaching the files:

\`\`\`bash
gh issue comment 116 --body-file ${comment_path}
gh pr comment 117 --body-file ${comment_path}
\`\`\`

Keep #116 open until these files are attached and the final device validation checkbox is checked.
EOF

  if [[ "$POST_GITHUB_COMMENT" -eq 1 ]]; then
    "$GH_BIN" issue comment 116 --body-file "$comment_path"
    "$GH_BIN" pr comment 117 --body-file "$comment_path"
    echo "Posted GitHub evidence comment to issue #116 and PR #117"
  fi

  echo
  echo "Final macOS validation evidence is ready:"
  echo "- Report: $report_path"
  echo "- Smoke log: $smoke_log_path"
  echo "- System evidence log: $system_evidence_log_path"
  echo "- GitHub evidence comment: $comment_path"
  echo "- Evidence bundle: $bundle_path"
  echo "- Evidence bundle checksum: $checksum_path"
  echo
  echo "Attach these files to PR #117 or the release checklist before closing #116."
}

if [[ -n "$CHECK_COMPLETE_PATH" ]]; then
  check_complete_report "$CHECK_COMPLETE_PATH"
  exit 0
fi

if [[ -n "$FINALIZE_PATH" ]]; then
  finalize_evidence "$FINALIZE_PATH"
  exit 0
fi

if [[ -n "$BUNDLE_EVIDENCE_PATH" ]]; then
  create_evidence_bundle "$BUNDLE_EVIDENCE_PATH"
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
SYSTEM_EVIDENCE_LOG_PATH="${MACOS_VALIDATION_SYSTEM_EVIDENCE_LOG:-build/macos-system-evidence-${TIMESTAMP}.log}"
mkdir -p "$(dirname "$REPORT_PATH")" "$(dirname "$SMOKE_LOG_PATH")" "$(dirname "$SYSTEM_EVIDENCE_LOG_PATH")"
VERSION="$("$PYTHON_BIN" -c 'from src.version import __version__; print(__version__)')"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
PYTHON_VERSION="$(python --version 2>&1 || echo unavailable)"
CARGO_VERSION="$(cargo --version 2>/dev/null || echo unavailable)"
MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null || echo unavailable)"
MACOS_BUILD="$(sw_vers -buildVersion 2>/dev/null || echo unavailable)"
ARCH="$(uname -m)"
ARTIFACT_ARCH="${ARTIFACT_ARCH_ARG:-${ARCH}}"
ARTIFACT_ENV_PATH="build/macos-validation-artifacts-${TIMESTAMP}.env"
ARTIFACT_OUTPUT_DIR="${ARTIFACT_OUTPUT_DIR_ARG:-${MACOS_VALIDATION_ARTIFACT_DIR:-build/macos-validation-artifacts-${GIT_SHA}-${ARTIFACT_ARCH}}}"
FINAL_APP_PATH="${MACOS_VALIDATION_APP_PATH:-/Applications/TunnelForge.app}"
FINAL_APP_EXECUTABLE="${FINAL_APP_PATH}/Contents/MacOS/TunnelForge"
ARTIFACT_WORKFLOW_RUN="${MACOS_VALIDATION_ARTIFACT_RUN_ID:-}"
ARTIFACT_DIR="${MACOS_VALIDATION_ARTIFACT_DIR:-build/macos-validation-artifacts}"
ARTIFACT_CHECKSUM_STATUS="${MACOS_VALIDATION_ARTIFACT_CHECKSUMS:-pending}"

write_system_evidence_log() {
  {
    echo "== sw_vers =="
    sw_vers || true
    echo
    echo "== uname =="
    uname -a || true
    echo
    echo "== architecture =="
    uname -m || true
    echo
    echo "== final app =="
    echo "$FINAL_APP_PATH"
    if [[ -e "$FINAL_APP_PATH" ]]; then
      ls -ld "$FINAL_APP_PATH" || true
    else
      echo "missing"
    fi
    echo
    echo "== final app executable =="
    echo "$FINAL_APP_EXECUTABLE"
    if [[ -e "$FINAL_APP_EXECUTABLE" ]]; then
      ls -l "$FINAL_APP_EXECUTABLE" || true
    else
      echo "missing"
    fi
    echo
    echo "== codesign verify =="
    set +e
    codesign --verify --deep --strict --verbose=2 "$FINAL_APP_PATH" 2>&1
    echo "exit: $?"
    echo
    echo "== spctl assess =="
    spctl --assess --type execute --verbose "$FINAL_APP_PATH" 2>&1
    echo "exit: $?"
    set -e
  } > "$SYSTEM_EVIDENCE_LOG_PATH"
}

if [[ "$DOWNLOAD_ARTIFACTS" -eq 1 ]]; then
  artifact_download_args=(
    --arch "$ARTIFACT_ARCH"
    --output-dir "$ARTIFACT_OUTPUT_DIR"
    --write-env "$ARTIFACT_ENV_PATH"
  )
  if [[ -n "$ARTIFACT_RUN_ID_ARG" ]]; then
    artifact_download_args+=(--run-id "$ARTIFACT_RUN_ID_ARG")
  fi

  bash scripts/macos-download-validation-artifacts.sh "${artifact_download_args[@]}"
  ARTIFACT_WORKFLOW_RUN="$(read_artifact_env_value "$ARTIFACT_ENV_PATH" MACOS_VALIDATION_ARTIFACT_RUN_ID)"
  ARTIFACT_DIR="$(read_artifact_env_value "$ARTIFACT_ENV_PATH" MACOS_VALIDATION_ARTIFACT_DIR)"
  ARTIFACT_CHECKSUM_STATUS="$(read_artifact_env_value "$ARTIFACT_ENV_PATH" MACOS_VALIDATION_ARTIFACT_CHECKSUMS)"
fi

SMOKE_STATUS="not run"
SMOKE_CHECK_MARK=" "
if [[ "$RUN_SMOKE" -eq 1 ]]; then
  set +e
  bash scripts/validate-macos-release.sh 2>&1 | tee "$SMOKE_LOG_PATH"
  smoke_exit="${PIPESTATUS[0]}"
  set -e

  if [[ "$smoke_exit" -eq 0 ]]; then
    SMOKE_STATUS="passed"
    SMOKE_CHECK_MARK="x"
  else
    SMOKE_STATUS="failed (exit ${smoke_exit})"
  fi
fi

ARTIFACT_CHECK_MARK=" "
if [[ "$ARTIFACT_CHECKSUM_STATUS" =~ ^(pass|passed|PASS|PASSED)$ ]]; then
  ARTIFACT_CHECK_MARK="x"
fi

write_system_evidence_log

cat > "$REPORT_PATH" <<EOF
# TunnelForge macOS Manual Validation Report

## Metadata

- UTC time: ${TIMESTAMP}
- TunnelForge version: ${VERSION}
- Git SHA: ${GIT_SHA}
- macOS: ${MACOS_VERSION} (${MACOS_BUILD})
- Architecture: ${ARCH}
- Artifact workflow run: ${ARTIFACT_WORKFLOW_RUN}
- Artifact directory: ${ARTIFACT_DIR}
- Artifact checksum verification: ${ARTIFACT_CHECKSUM_STATUS}
- Python: ${PYTHON_VERSION}
- Cargo: ${CARGO_VERSION}
- Release smoke: ${SMOKE_STATUS}
- Smoke log: ${SMOKE_LOG_PATH}
- System evidence log: ${SYSTEM_EVIDENCE_LOG_PATH}
- Final app path: ${FINAL_APP_PATH}
- Final app executable: ${FINAL_APP_EXECUTABLE}
- Applications install smoke: set \`MACOS_RELEASE_SMOKE_APPLICATIONS=1\` before \`--run-smoke\` to include it in the smoke log
- Completion check: \`bash scripts/macos-manual-validation-report.sh --check-complete ${REPORT_PATH}\`
- Evidence bundle: \`bash scripts/macos-manual-validation-report.sh --bundle-evidence ${REPORT_PATH}\`
- Artifact download: \`bash scripts/macos-download-validation-artifacts.sh --run-id ${ARTIFACT_WORKFLOW_RUN:-<workflow-run-id>} --arch ${ARTIFACT_ARCH} --output-dir ${ARTIFACT_DIR}\`

## Automated Smoke

- [${SMOKE_CHECK_MARK}] Run \`bash scripts/validate-macos-release.sh\`
- Optional \`/Applications\` install smoke is included when \`MACOS_RELEASE_SMOKE_APPLICATIONS=1\` is set before \`--run-smoke\`.
- [${SMOKE_CHECK_MARK}] Confirm source \`python main.py --ui-smoke-check\` passed
- [${ARTIFACT_CHECK_MARK}] Download signed/notarized GitHub Actions macOS artifacts
- [${ARTIFACT_CHECK_MARK}] Confirm downloaded macOS artifact checksums passed
- [${SMOKE_CHECK_MARK}] Confirm built app smoke passed
- [${SMOKE_CHECK_MARK}] Confirm mounted DMG smoke passed
- [${SMOKE_CHECK_MARK}] Confirm copied DMG install smoke passed
- [${SMOKE_CHECK_MARK}] Confirm ZIP extracted app smoke passed

## Interactive App Launch

- [ ] Launch \`python main.py\`
- [ ] Launch \`dist/TunnelForge.app\`
- [ ] Install from DMG into \`/Applications\` and launch \`${FINAL_APP_PATH}\`
- [ ] Confirm \`tunnelforge-core\` starts from inside the app

## SSH Tunnel

- [ ] Create an SSH tunnel
- [ ] Confirm tunnel monitoring updates
- Optional: record reconnect behavior if applicable
- [ ] Close the SSH tunnel cleanly

## Database Connections

- [ ] Test MySQL connection through Rust DB Core
- [ ] Test PostgreSQL connection through Rust DB Core
- Optional: record direct connection mode if applicable

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
echo "Then run: bash scripts/macos-manual-validation-report.sh --bundle-evidence $REPORT_PATH"
echo "Or run both plus the final GitHub gate: bash scripts/macos-manual-validation-report.sh --finalize $REPORT_PATH"
