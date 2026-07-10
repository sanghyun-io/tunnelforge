"""Shared release-package integrity verification."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re


SHA256_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class IntegrityError(ValueError):
    pass


def parse_sha256_digest(raw: object) -> str:
    match = SHA256_DIGEST_RE.fullmatch(str(raw or "").strip())
    if not match:
        raise IntegrityError("release asset SHA-256 digest is missing or invalid")
    return match.group(1).lower()


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
