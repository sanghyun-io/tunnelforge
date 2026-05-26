#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  VERSION="$(python -c 'from src.version import __version__; print(__version__)')"
fi

ARCH_NAME="${MACOS_PACKAGE_ARCH:-$(uname -m)}"
HOST_ARCH="$(uname -m)"
APP_PATH="dist/TunnelForge.app"
ZIP_PATH="dist/TunnelForge-macOS-${VERSION}-${ARCH_NAME}.zip"
DMG_STAGING="build/macos-dmg"
DMG_PATH="dist/TunnelForge-macOS-${VERSION}-${ARCH_NAME}.dmg"

create_dmg_with_retry() {
  local attempt
  local max_attempts=3

  for attempt in $(seq 1 "$max_attempts"); do
    rm -f "$DMG_PATH"
    if hdiutil create -volname "TunnelForge" \
      -srcfolder "$DMG_STAGING" \
      -ov -format UDZO "$DMG_PATH"; then
      return 0
    fi

    echo "hdiutil create failed on attempt ${attempt}/${max_attempts}." >&2
    rm -f "$DMG_PATH"
    hdiutil info || true
    if [[ "$attempt" -lt "$max_attempts" ]]; then
      sleep $((attempt * 2))
    fi
  done

  echo "Failed to create $DMG_PATH after $max_attempts attempts." >&2
  return 1
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

if [[ ! -d "$APP_PATH" ]]; then
  echo "Missing $APP_PATH. Run scripts/build-macos.sh first." >&2
  exit 1
fi

if [[ "$ARCH_NAME" != "$HOST_ARCH" ]]; then
  echo "Host architecture $HOST_ARCH does not match requested package architecture $ARCH_NAME." >&2
  exit 1
fi

notarization_env_count=0
[[ -n "${APPLE_ID:-}" ]] && notarization_env_count=$((notarization_env_count + 1))
[[ -n "${APPLE_TEAM_ID:-}" ]] && notarization_env_count=$((notarization_env_count + 1))
[[ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]] && notarization_env_count=$((notarization_env_count + 1))

if [[ "$notarization_env_count" -gt 0 && "$notarization_env_count" -lt 3 ]]; then
  echo "Apple notarization credentials are incomplete; set APPLE_ID, APPLE_TEAM_ID, and APPLE_APP_SPECIFIC_PASSWORD together." >&2
  exit 1
fi

if [[ "$notarization_env_count" -eq 3 && -z "${APPLE_CODESIGN_IDENTITY:-}" ]]; then
  echo "APPLE_CODESIGN_IDENTITY is required before notarization." >&2
  exit 1
fi

if [[ -n "${APPLE_CODESIGN_IDENTITY:-}" ]]; then
  echo "Signing $APP_PATH"
  codesign --force --deep --options runtime --timestamp \
    --sign "$APPLE_CODESIGN_IDENTITY" "$APP_PATH"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
  echo "APPLE_CODESIGN_IDENTITY is not set; skipping codesign."
fi

mkdir -p dist
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
ditto "$APP_PATH" "$DMG_STAGING/TunnelForge.app"
ln -s /Applications "$DMG_STAGING/Applications"
create_dmg_with_retry

shasum -a 256 "$ZIP_PATH" > "$ZIP_PATH.sha256"

if [[ -n "${APPLE_CODESIGN_IDENTITY:-}" ]]; then
  echo "Signing $DMG_PATH"
  codesign --force --timestamp --sign "$APPLE_CODESIGN_IDENTITY" "$DMG_PATH"
fi

if [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]]; then
  echo "Submitting $DMG_PATH for notarization"
  xcrun notarytool submit "$DMG_PATH" \
    --apple-id "$APPLE_ID" \
    --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_SPECIFIC_PASSWORD" \
    --wait
  xcrun stapler staple "$DMG_PATH"
else
  echo "Apple notarization credentials are not set; skipping notarization."
fi

shasum -a 256 "$DMG_PATH" > "$DMG_PATH.sha256"

echo "Created $ZIP_PATH"
echo "Created $ZIP_PATH.sha256"
echo "Created $DMG_PATH"
echo "Created $DMG_PATH.sha256"
