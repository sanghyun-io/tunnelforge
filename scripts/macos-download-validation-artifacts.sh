#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PR_NUMBER=117
WORKFLOW_FILE="macos-app.yml"
WORKFLOW_NAME="macOS App Validation"
WORKFLOW_EVENT="workflow_dispatch"
GH_BIN="${GH:-gh}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/macos-download-validation-artifacts.sh [options]
  bash scripts/macos-download-validation-artifacts.sh --verify-only <artifact-dir>

Options:
  --run-id <id>          Download artifacts from a specific macOS App Validation run.
                         Defaults to the latest successful manual workflow_dispatch run for PR #117 head.
  --repo <owner/name>    GitHub repository. Defaults to `gh repo view`.
  --arch <arch|all>      Artifact architecture to download: arm64, x86_64, or all. Default: all.
  --output-dir <dir>     Destination directory. Default: build/macos-validation-artifacts.
  --skip-verify          Download without verifying .sha256 files.
  --verify-only <dir>    Only verify already-downloaded artifacts under <dir>.
  --write-env <file>     Write MACOS_VALIDATION_ARTIFACT_* variables for the report script.
  --help                 Show this help.
EOF
}

RUN_ID=""
REPO=""
ARCH_FILTER="all"
OUTPUT_DIR="build/macos-validation-artifacts"
VERIFY_ONLY_DIR=""
SKIP_VERIFY=0
WRITE_ENV_PATH=""

normalize_path() {
  local path="$1"
  local drive=""
  if [[ "$path" =~ ^([A-Za-z]):/(.*)$ ]] && [[ -r /proc/version ]] && grep -qi microsoft /proc/version; then
    drive="$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:upper:]' '[:lower:]')"
    echo "/mnt/${drive}/${BASH_REMATCH[2]}"
    return
  fi

  if command -v cygpath >/dev/null 2>&1 && [[ "$path" =~ ^[A-Za-z]:[\\/] ]]; then
    cygpath -u "$path"
    return
  fi

  if [[ -e "$path" ]]; then
    echo "$path"
    return
  fi

  echo "$path"
}

lowercase() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

require_gh() {
  if ! command -v "$GH_BIN" >/dev/null 2>&1; then
    echo "gh is required to download macOS validation artifacts." >&2
    exit 1
  fi
}

resolve_repo() {
  if [[ -n "$REPO" ]]; then
    echo "$REPO"
    return
  fi

  "$GH_BIN" repo view --json nameWithOwner --jq .nameWithOwner
}

resolve_run_id() {
  local repo="$1"
  local pr_head_sha=""
  local run_id=""

  if [[ -n "$RUN_ID" ]]; then
    echo "$RUN_ID"
    return
  fi

  pr_head_sha="$("$GH_BIN" pr view "$PR_NUMBER" --repo "$repo" --json headRefOid --jq .headRefOid)"
  run_id="$(
    "$GH_BIN" api "repos/${repo}/actions/workflows/${WORKFLOW_FILE}/runs?event=${WORKFLOW_EVENT}&status=success&per_page=20" \
      --jq ".workflow_runs[] | select(.head_sha == \"${pr_head_sha}\") | .id" \
      | head -n 1
  )"

  if [[ -z "$run_id" ]]; then
    echo "No successful manual ${WORKFLOW_NAME} ${WORKFLOW_EVENT} run found for PR #${PR_NUMBER} head ${pr_head_sha}." >&2
    exit 1
  fi

  echo "$run_id"
}

verify_checksum_file() {
  local checksum_file="$1"
  local checksum_dir=""
  local expected=""
  local recorded_path=""
  local artifact_name=""
  local artifact_path=""
  local actual=""
  local actual_lower=""
  local expected_lower=""

  checksum_dir="$(dirname "$checksum_file")"

  while read -r expected recorded_path _; do
    expected="${expected%$'\r'}"
    recorded_path="${recorded_path%$'\r'}"
    if [[ -z "${expected:-}" || "$expected" == \#* ]]; then
      continue
    fi

    artifact_name="$(basename "$recorded_path")"
    artifact_path="${checksum_dir}/${artifact_name}"
    if [[ ! -f "$artifact_path" ]]; then
      echo "Checksum target is missing for ${checksum_file}: ${artifact_path}" >&2
      return 1
    fi

    actual="$(shasum -a 256 "$artifact_path" | awk '{print $1}')"
    actual_lower="$(lowercase "$actual")"
    expected_lower="$(lowercase "$expected")"
    if [[ "$actual_lower" != "$expected_lower" ]]; then
      echo "Checksum mismatch for ${artifact_path}: expected ${expected}, got ${actual}" >&2
      return 1
    fi

    echo "Checksum verified: ${artifact_path}"
  done < "$checksum_file"
}

verify_downloaded_checksums() {
  local artifact_root=""
  local checksum_count=0

  artifact_root="$(normalize_path "$1")"
  if [[ ! -d "$artifact_root" ]]; then
    echo "Artifact directory not found: $artifact_root" >&2
    exit 1
  fi

  while IFS= read -r -d '' checksum_file; do
    verify_checksum_file "$checksum_file"
    checksum_count=$((checksum_count + 1))
  done < <(find "$artifact_root" -type f -name '*.sha256' -print0)

  if [[ "$checksum_count" -eq 0 ]]; then
    echo "No .sha256 files found under $artifact_root." >&2
    exit 1
  fi
}

write_report_env_file() {
  local env_path="$1"
  local run_id="$2"
  local artifact_dir="$3"
  local checksum_status="$4"

  if [[ -z "$env_path" ]]; then
    return
  fi

  mkdir -p "$(dirname "$env_path")"
  {
    printf 'MACOS_VALIDATION_ARTIFACT_RUN_ID=%s\n' "$run_id"
    printf 'MACOS_VALIDATION_ARTIFACT_DIR=%s\n' "$artifact_dir"
    printf 'MACOS_VALIDATION_ARTIFACT_CHECKSUMS=%s\n' "$checksum_status"
  } > "$env_path"
  echo "Wrote macOS validation artifact environment: $env_path"
}

clean_existing_artifacts() {
  local output_dir="$1"
  local artifact_pattern="$2"

  mkdir -p "$output_dir"
  while IFS= read -r -d '' artifact_path; do
    rm -rf "$artifact_path"
  done < <(find "$output_dir" -mindepth 1 -maxdepth 1 -name "$artifact_pattern" -print0)
}

download_artifacts() {
  local repo=""
  local run_id=""
  local artifact_pattern=""
  local checksum_status="skipped"

  require_gh
  repo="$(resolve_repo)"
  run_id="$(resolve_run_id "$repo")"

  if [[ "$ARCH_FILTER" == "all" ]]; then
    artifact_pattern="TunnelForge-macOS-*"
  else
    artifact_pattern="TunnelForge-macOS-*-${ARCH_FILTER}"
  fi
  clean_existing_artifacts "$OUTPUT_DIR" "$artifact_pattern"

  echo "Downloading ${WORKFLOW_NAME} artifacts from run ${run_id}: ${artifact_pattern}"
  "$GH_BIN" run download "$run_id" --repo "$repo" --dir "$OUTPUT_DIR" --pattern "$artifact_pattern"

  if [[ "$SKIP_VERIFY" -ne 1 ]]; then
    verify_downloaded_checksums "$OUTPUT_DIR"
    checksum_status="passed"
  fi

  write_report_env_file "$WRITE_ENV_PATH" "$run_id" "$OUTPUT_DIR" "$checksum_status"

  echo
  echo "macOS validation artifacts are ready under $OUTPUT_DIR"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      if [[ -z "${2:-}" ]]; then
        echo "--run-id requires a run id." >&2
        exit 2
      fi
      RUN_ID="$2"
      shift 2
      ;;
    --repo)
      if [[ -z "${2:-}" ]]; then
        echo "--repo requires owner/name." >&2
        exit 2
      fi
      REPO="$2"
      shift 2
      ;;
    --arch)
      if [[ -z "${2:-}" ]]; then
        echo "--arch requires arm64, x86_64, or all." >&2
        exit 2
      fi
      ARCH_FILTER="$2"
      shift 2
      ;;
    --output-dir)
      if [[ -z "${2:-}" ]]; then
        echo "--output-dir requires a directory." >&2
        exit 2
      fi
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --skip-verify)
      SKIP_VERIFY=1
      shift
      ;;
    --verify-only)
      if [[ -z "${2:-}" ]]; then
        echo "--verify-only requires an artifact directory." >&2
        exit 2
      fi
      VERIFY_ONLY_DIR="$2"
      shift 2
      ;;
    --write-env)
      if [[ -z "${2:-}" ]]; then
        echo "--write-env requires a file path." >&2
        exit 2
      fi
      WRITE_ENV_PATH="$2"
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

case "$ARCH_FILTER" in
  arm64|x86_64|all)
    ;;
  *)
    echo "--arch must be arm64, x86_64, or all." >&2
    exit 2
    ;;
esac

if [[ -n "$VERIFY_ONLY_DIR" ]]; then
  verify_downloaded_checksums "$VERIFY_ONLY_DIR"
  exit 0
fi

download_artifacts
