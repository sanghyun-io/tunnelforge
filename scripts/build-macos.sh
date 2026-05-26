#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"

echo "[1/5] Checking macOS build host"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

echo "[2/5] Building Rust tunnelforge-core"
cargo build --manifest-path migration_core/Cargo.toml --release

if [[ ! -f "migration_core/target/release/tunnelforge-core" ]]; then
  echo "Missing migration_core/target/release/tunnelforge-core" >&2
  exit 1
fi

echo "[3/5] Preparing macOS icon"
if [[ ! -f "assets/icon.icns" ]]; then
  if [[ -f "assets/icon_512.png" ]]; then
    mkdir -p build/icon.iconset
    sips -z 16 16 assets/icon_512.png --out build/icon.iconset/icon_16x16.png >/dev/null
    sips -z 32 32 assets/icon_512.png --out build/icon.iconset/icon_16x16@2x.png >/dev/null
    sips -z 32 32 assets/icon_512.png --out build/icon.iconset/icon_32x32.png >/dev/null
    sips -z 64 64 assets/icon_512.png --out build/icon.iconset/icon_32x32@2x.png >/dev/null
    sips -z 128 128 assets/icon_512.png --out build/icon.iconset/icon_128x128.png >/dev/null
    sips -z 256 256 assets/icon_512.png --out build/icon.iconset/icon_128x128@2x.png >/dev/null
    sips -z 256 256 assets/icon_512.png --out build/icon.iconset/icon_256x256.png >/dev/null
    sips -z 512 512 assets/icon_512.png --out build/icon.iconset/icon_256x256@2x.png >/dev/null
    sips -z 512 512 assets/icon_512.png --out build/icon.iconset/icon_512x512.png >/dev/null
    iconutil -c icns build/icon.iconset -o assets/icon.icns
  else
    echo "Missing assets/icon.icns and assets/icon_512.png" >&2
    exit 1
  fi
fi

echo "[4/5] Building TunnelForge.app with PyInstaller"
python -m PyInstaller tunnel-manager.spec --noconfirm

if [[ ! -d "dist/TunnelForge.app" ]]; then
  echo "Missing dist/TunnelForge.app" >&2
  exit 1
fi

MINIMUM_MACOS_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "dist/TunnelForge.app/Contents/Info.plist")"
if [[ "$MINIMUM_MACOS_VERSION" != "13.0" ]]; then
  echo "Minimum macOS version must be 13.0; found $MINIMUM_MACOS_VERSION" >&2
  exit 1
fi

if ! find "dist/TunnelForge.app" -type f -name "tunnelforge-core" | grep -q .; then
  echo "Missing bundled tunnelforge-core in dist/TunnelForge.app" >&2
  exit 1
fi

echo "[5/5] macOS build complete: dist/TunnelForge.app"
