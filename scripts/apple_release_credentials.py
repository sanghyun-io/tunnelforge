"""Classify Apple release credentials without exposing secret values."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


REQUIRED_CREDENTIALS = (
    "APPLE_CODESIGN_CERTIFICATE_P12_BASE64",
    "APPLE_CODESIGN_CERTIFICATE_PASSWORD",
    "APPLE_ID",
    "APPLE_TEAM_ID",
    "APPLE_APP_SPECIFIC_PASSWORD",
)

OPTIONAL_CREDENTIALS = (
    "APPLE_CODESIGN_IDENTITY",
    "APPLE_CODESIGN_KEYCHAIN_PASSWORD",
)


def determine_apple_release_mode(values: Mapping[str, str]) -> str:
    configured = {
        name
        for name in (*REQUIRED_CREDENTIALS, *OPTIONAL_CREDENTIALS)
        if values.get(name, "").strip()
    }
    if not configured:
        return "unsigned"

    missing = [name for name in REQUIRED_CREDENTIALS if name not in configured]
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"incomplete Apple release credentials; missing: {names}")

    return "signed"


def main() -> int:
    try:
        print(determine_apple_release_mode(os.environ))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
