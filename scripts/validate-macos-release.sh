#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

VERSION="${1:-$(python -c 'from src.version import __version__; print(__version__)')}"
ARCH_NAME="${MACOS_PACKAGE_ARCH:-$(uname -m)}"
APP_EXECUTABLE="dist/TunnelForge.app/Contents/MacOS/TunnelForge"
DMG_PATH="dist/TunnelForge-macOS-${VERSION}-${ARCH_NAME}.dmg"
ZIP_PATH="dist/TunnelForge-macOS-${VERSION}-${ARCH_NAME}.zip"

validate_smoke_response() {
  local response="$1"
  RESPONSE="$response" python - <<'PY'
import json
import os

data = json.loads(os.environ["RESPONSE"])
assert data["success"] is True
assert data["window_title"] == "TunnelForge"
self_check = data["self_check"]
assert self_check["icon_exists"] is True
assert self_check["core_exists"] is True
assert self_check["core_hello"]["service"] == "tunnelforge-core"
assert self_check["core_hello"]["event"] == "result"
PY
}

smoke_app() {
  local executable="$1"
  test -x "$executable"
  local response
  response="$("$executable" --ui-smoke-check)"
  echo "$response"
  validate_smoke_response "$response"
}

echo "[1/8] Building Rust core for source-run smoke"
cargo build --manifest-path migration_core/Cargo.toml --release
test -f "migration_core/target/release/tunnelforge-core"

echo "[2/8] Smoke testing source-run app"
source_response="$(python main.py --ui-smoke-check)"
echo "$source_response"
validate_smoke_response "$source_response"

echo "[3/8] Building macOS app"
bash scripts/build-macos.sh

echo "[4/8] Smoke testing built app"
smoke_app "$APP_EXECUTABLE"

echo "[5/8] Packaging DMG and ZIP"
bash scripts/package-macos.sh "$VERSION"
test -f "$DMG_PATH"
test -f "$ZIP_PATH"

echo "[6/8] Smoke testing DMG package"
hdiutil attach "$DMG_PATH" -mountpoint /Volumes/TunnelForge -quiet
trap 'hdiutil detach /Volumes/TunnelForge -quiet || true' EXIT
smoke_app "/Volumes/TunnelForge/TunnelForge.app/Contents/MacOS/TunnelForge"
hdiutil detach /Volumes/TunnelForge -quiet
trap - EXIT

echo "[7/8] Smoke testing copied DMG install"
hdiutil attach "$DMG_PATH" -mountpoint /Volumes/TunnelForge -quiet
trap 'hdiutil detach /Volumes/TunnelForge -quiet || true' EXIT
rm -rf build/install-smoke
mkdir -p build/install-smoke
ditto "/Volumes/TunnelForge/TunnelForge.app" "build/install-smoke/TunnelForge.app"
hdiutil detach /Volumes/TunnelForge -quiet
trap - EXIT
smoke_app "build/install-smoke/TunnelForge.app/Contents/MacOS/TunnelForge"

echo "[8/8] Smoke testing ZIP package"
rm -rf build/zip-smoke
mkdir -p build/zip-smoke
ditto -x -k "$ZIP_PATH" build/zip-smoke
smoke_app "build/zip-smoke/TunnelForge.app/Contents/MacOS/TunnelForge"

cat <<'EOF'
macOS release package smoke checks passed.

Manual validation still required before production-ready macOS support:
- Interactively launch from source with python main.py.
- Launch installed TunnelForge.app from Applications.
- Create and close an SSH tunnel.
- Test MySQL and PostgreSQL connections.
- Run Export/Import on disposable databases.
- Run migration inspect, preflight, plan, migrate, verify, and resume.
- Enable and disable startup and inspect the LaunchAgent.
- Confirm update package opening, Gatekeeper, signing, and notarization behavior.
EOF
