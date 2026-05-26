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
rm -f "$DMG_PATH"
hdiutil create -volname "TunnelForge" \
  -srcfolder "$DMG_STAGING" \
  -ov -format UDZO "$DMG_PATH"

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

echo "Created $ZIP_PATH"
echo "Created $DMG_PATH"
