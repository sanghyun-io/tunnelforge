#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

APP_PATH="${1:-build/install-smoke/TunnelForge.app}"
APP_EXECUTABLE="${APP_PATH}/Contents/MacOS/TunnelForge"
SMOKE_HOME="${MACOS_LAUNCHAGENT_SMOKE_HOME:-$PWD/build/launchagent-smoke-home}"
PLIST_PATH="${SMOKE_HOME}/Library/LaunchAgents/io.sanghyun.tunnelforge.plist"
LOG_DIR="${SMOKE_HOME}/Library/Logs/TunnelForge"

if [[ ! -x "$APP_EXECUTABLE" ]]; then
  echo "Packaged app executable not found: $APP_EXECUTABLE" >&2
  exit 1
fi

rm -rf "$SMOKE_HOME"
mkdir -p "$SMOKE_HOME"

SMOKE_HOME="$SMOKE_HOME" APP_EXECUTABLE="$APP_EXECUTABLE" python - <<'PY'
import os
import plistlib
import sys
from pathlib import Path

from src.core.platform_integration import StartupRegistrar

home = Path(os.environ["SMOKE_HOME"])
app_executable = os.environ["APP_EXECUTABLE"]
sys.frozen = True

registrar = StartupRegistrar(
    platform_name="Darwin",
    home=home,
    executable=app_executable,
)

assert registrar.is_supported is True
assert registrar.is_registered() is False

ok, message = registrar.set_registered(True)
assert ok, message
assert registrar.is_registered() is True

plist_path = home / "Library" / "LaunchAgents" / "io.sanghyun.tunnelforge.plist"
launch_agent = plistlib.loads(plist_path.read_bytes())
assert launch_agent["Label"] == "io.sanghyun.tunnelforge"
assert launch_agent["ProgramArguments"] == [app_executable, "--minimized"]
assert launch_agent["RunAtLoad"] is True
assert launch_agent["StandardOutPath"] == str(
    home / "Library" / "Logs" / "TunnelForge" / "launchagent.out.log"
)
assert launch_agent["StandardErrorPath"] == str(
    home / "Library" / "Logs" / "TunnelForge" / "launchagent.err.log"
)
assert (home / "Library" / "Logs" / "TunnelForge").is_dir()
PY

plutil -lint "$PLIST_PATH"

if [[ "${MACOS_LAUNCHAGENT_BOOTSTRAP:-0}" == "1" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH"
fi

SMOKE_HOME="$SMOKE_HOME" APP_EXECUTABLE="$APP_EXECUTABLE" python - <<'PY'
import os
import sys
from pathlib import Path

from src.core.platform_integration import StartupRegistrar

home = Path(os.environ["SMOKE_HOME"])
app_executable = os.environ["APP_EXECUTABLE"]
sys.frozen = True

registrar = StartupRegistrar(
    platform_name="Darwin",
    home=home,
    executable=app_executable,
)

ok, message = registrar.set_registered(False)
assert ok, message
assert registrar.is_registered() is False
assert not (home / "Library" / "LaunchAgents" / "io.sanghyun.tunnelforge.plist").exists()
PY

echo "macOS LaunchAgent smoke checks passed."
