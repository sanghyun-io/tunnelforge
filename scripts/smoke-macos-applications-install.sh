#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

DMG_PATH="${1:-}"
TARGET_APP="${2:-/Applications/TunnelForge.app}"

if [[ -z "$DMG_PATH" ]]; then
  echo "Usage: bash scripts/smoke-macos-applications-install.sh <TunnelForge.dmg> [target-app]" >&2
  exit 2
fi

if [[ ! -f "$DMG_PATH" ]]; then
  echo "DMG not found: $DMG_PATH" >&2
  exit 1
fi

if [[ "$TARGET_APP" == /Applications/* && "${MACOS_APPLICATIONS_SMOKE_ALLOW_SYSTEM:-}" != "1" ]]; then
  echo "Refusing to modify /Applications without MACOS_APPLICATIONS_SMOKE_ALLOW_SYSTEM=1." >&2
  exit 1
fi

MOUNT_PATH="$PWD/build/applications-smoke-mount"
MOUNTED=0

remove_target_app() {
  if [[ -e "$TARGET_APP" ]]; then
    if [[ "$TARGET_APP" == /Applications/* ]]; then
      sudo rm -rf "$TARGET_APP"
    else
      rm -rf "$TARGET_APP"
    fi
  fi
}

cleanup() {
  if [[ "$MOUNTED" -eq 1 ]]; then
    hdiutil detach "$MOUNT_PATH" -quiet || true
  fi
  remove_target_app
}
trap cleanup EXIT

rm -rf "$MOUNT_PATH"
mkdir -p "$MOUNT_PATH" "$(dirname "$TARGET_APP")"
hdiutil attach "$DMG_PATH" -mountpoint "$MOUNT_PATH" -quiet
MOUNTED=1

SOURCE_APP="$MOUNT_PATH/TunnelForge.app"
if [[ ! -d "$SOURCE_APP" ]]; then
  echo "Mounted DMG does not contain TunnelForge.app: $DMG_PATH" >&2
  exit 1
fi

remove_target_app
if [[ "$TARGET_APP" == /Applications/* ]]; then
  sudo ditto "$SOURCE_APP" "$TARGET_APP"
else
  ditto "$SOURCE_APP" "$TARGET_APP"
fi

APP_EXECUTABLE="$TARGET_APP/Contents/MacOS/TunnelForge"
test -x "$APP_EXECUTABLE"
RESPONSE="$("$APP_EXECUTABLE" --ui-smoke-check)"
echo "$RESPONSE"
RESPONSE="$RESPONSE" python - <<'PY'
import json
import os

data = json.loads(os.environ["RESPONSE"])
assert data["success"] is True
assert data["window_title"] == "TunnelForge"
self_check = data["self_check"]
assert self_check["icon_exists"] is True
assert self_check["core_exists"] is True
assert self_check["core_hello"]["service"] == "tunnelforge-core"
assert self_check["core_hello"]["success"] is True
PY

bash scripts/smoke-macos-launchagent.sh "$TARGET_APP"

echo "macOS /Applications install smoke checks passed."
