#!/usr/bin/env bash
# Sourced helpers for attaching/detaching a DMG in CI and release smoke tests.
#
# A bare quiet `hdiutil attach` fails without any output when the previous
# step's volume is still being torn down ("Resource busy"), which made the macOS
# DMG smoke steps fail intermittently with no diagnostics. These helpers retry
# with a short backoff and, when everything fails, print hdiutil's own output and
# `hdiutil info` so the cause is visible in the log.
#
#   source scripts/macos-dmg-mount.sh
#   dmg_attach "$DMG_PATH" "$MOUNT_DIR"
#   trap 'dmg_detach "$MOUNT_DIR" || true' EXIT

DMG_MOUNT_MAX_ATTEMPTS="${DMG_MOUNT_MAX_ATTEMPTS:-5}"

dmg_attach() {
  local dmg_path="$1" mount_path="$2" attempt output
  for attempt in $(seq 1 "$DMG_MOUNT_MAX_ATTEMPTS"); do
    if output="$(hdiutil attach "$dmg_path" -mountpoint "$mount_path" -nobrowse 2>&1)"; then
      return 0
    fi
    echo "hdiutil attach failed on attempt ${attempt}/${DMG_MOUNT_MAX_ATTEMPTS}: ${output}" >&2
    if [[ "$attempt" -lt "$DMG_MOUNT_MAX_ATTEMPTS" ]]; then
      sleep $((attempt * 2))
    fi
  done
  echo "Failed to attach $dmg_path at $mount_path after $DMG_MOUNT_MAX_ATTEMPTS attempts." >&2
  hdiutil info >&2 || true
  return 1
}

dmg_detach() {
  local mount_path="$1" attempt output
  for attempt in 1 2 3; do
    if output="$(hdiutil detach "$mount_path" 2>&1)"; then
      return 0
    fi
    echo "hdiutil detach failed on attempt ${attempt}/3: ${output}" >&2
    sleep "$attempt"
  done
  # Last resort: the volume is busy; force it so the next step can attach again.
  if output="$(hdiutil detach "$mount_path" -force 2>&1)"; then
    return 0
  fi
  echo "hdiutil detach -force failed: ${output}" >&2
  hdiutil info >&2 || true
  return 1
}
