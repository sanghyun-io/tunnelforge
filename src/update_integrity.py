"""Shared release-package integrity verification."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
from typing import Optional


SHA256_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")

# Release installers are expected to remain well below this 2 GiB safety cap.
MAX_INSTALLER_SIZE = 2 * 1024 * 1024 * 1024


class IntegrityError(ValueError):
    pass


def parse_sha256_digest(raw: object) -> str:
    match = SHA256_DIGEST_RE.fullmatch(str(raw or "").strip())
    if not match:
        raise IntegrityError("release asset SHA-256 digest is missing or invalid")
    return match.group(1).lower()


def parse_content_length(raw: object) -> Optional[int]:
    """Return a canonical Content-Length value, or None for invalid headers."""
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def verify_file_integrity(
    path: str | os.PathLike[str], expected_sha256: str, expected_size: int
) -> None:
    path = Path(path)
    if expected_size <= 0:
        raise IntegrityError("release asset size must be positive")
    if path.stat().st_size != expected_size:
        raise IntegrityError("downloaded file size does not match release metadata")

    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)

    if not hmac.compare_digest(hasher.hexdigest(), expected_sha256):
        raise IntegrityError(
            "downloaded file SHA-256 does not match release metadata"
        )
