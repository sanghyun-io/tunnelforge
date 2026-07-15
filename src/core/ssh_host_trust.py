"""Persistent SSH server host-key trust decisions."""

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import paramiko

from src.core.platform_paths import ssh_host_trust_file


_SCHEMA_VERSION = 1
_FINGERPRINT_PATTERN = re.compile(r"^SHA256:[A-Za-z0-9+/]+$")
_HOST_FIELDS = {"host", "port", "key_type", "fingerprint_sha256"}


class SshHostTrustStoreError(Exception):
    """Raised when the SSH host trust store cannot be read or written safely."""


class SshHostKeyChangedError(SshHostTrustStoreError):
    """Raised when an approval would replace an existing host identity."""


@dataclass(frozen=True)
class SshHostKeyCheck:
    status: str
    host: str
    port: int
    key_type: str
    fingerprint_sha256: str
    previous_fingerprint_sha256: Optional[str] = None
    approval_token: Optional[str] = field(
        default=None, repr=False, compare=False
    )


def _fsync_parent_directory(parent: Path) -> None:
    if os.name != "posix":
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(str(parent), flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class SshHostKeyTrustStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else ssh_host_trust_file()
        self._memory_hosts = None

    @classmethod
    def in_memory(cls):
        store = cls.__new__(cls)
        store.path = None
        store._memory_hosts = []
        return store

    def check(self, host: str, port: int, key: paramiko.PKey) -> SshHostKeyCheck:
        key_type = key.get_name()
        fingerprint = self._fingerprint_sha256(key)
        endpoint = (host, int(port))

        for saved in self._read_hosts():
            if (saved["host"], saved["port"]) != endpoint:
                continue
            if (
                saved["key_type"] == key_type
                and saved["fingerprint_sha256"] == fingerprint
            ):
                status = "trusted"
                previous_fingerprint = None
            else:
                status = "changed"
                previous_fingerprint = saved["fingerprint_sha256"]
            return SshHostKeyCheck(
                status=status,
                host=host,
                port=int(port),
                key_type=key_type,
                fingerprint_sha256=fingerprint,
                previous_fingerprint_sha256=previous_fingerprint,
            )

        return SshHostKeyCheck(
            status="approval_required",
            host=host,
            port=int(port),
            key_type=key_type,
            fingerprint_sha256=fingerprint,
        )

    def approve(self, check: SshHostKeyCheck, key: paramiko.PKey) -> None:
        if not isinstance(check, SshHostKeyCheck):
            raise SshHostTrustStoreError("Invalid SSH host-key check")

        current = self.check(check.host, check.port, key)
        if check.status == "changed" or current.status == "changed":
            raise SshHostKeyChangedError("Changed SSH host keys cannot be approved")
        if (
            check.status != "approval_required"
            or self._check_identity(check) != self._check_identity(current)
        ):
            raise SshHostTrustStoreError(
                "SSH host-key check does not match the supplied key"
            )

        hosts = self._read_hosts()
        endpoint = (check.host, check.port)
        for saved in hosts:
            if (saved["host"], saved["port"]) != endpoint:
                continue
            raise SshHostKeyChangedError(
                "Stored SSH host key changed before approval"
            )

        hosts.append(
            {
                "host": check.host,
                "port": check.port,
                "key_type": check.key_type,
                "fingerprint_sha256": check.fingerprint_sha256,
            }
        )
        self._write_hosts(hosts)

    @staticmethod
    def _check_identity(check: SshHostKeyCheck):
        return (
            check.status,
            check.host,
            check.port,
            check.key_type,
            check.fingerprint_sha256,
            check.previous_fingerprint_sha256,
        )

    @staticmethod
    def _fingerprint_sha256(key: paramiko.PKey) -> str:
        digest = hashlib.sha256(key.asbytes()).digest()
        encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
        return f"SHA256:{encoded}"

    def _read_hosts(self) -> List[Dict[str, object]]:
        if self._memory_hosts is not None:
            return [dict(host) for host in self._memory_hosts]
        if not self.path.exists():
            return []

        try:
            with self.path.open("r", encoding="utf-8") as trust_file:
                payload = json.load(trust_file)
            return self._validate_payload(payload)
        except SshHostTrustStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SshHostTrustStoreError(
                "SSH host trust store could not be read"
            ) from exc

    @staticmethod
    def _validate_payload(payload) -> List[Dict[str, object]]:
        if not isinstance(payload, dict) or set(payload) != {"version", "hosts"}:
            raise SshHostTrustStoreError("Invalid SSH host trust store schema")
        if payload["version"] != _SCHEMA_VERSION:
            raise SshHostTrustStoreError("Unsupported SSH host trust store version")
        if not isinstance(payload["hosts"], list):
            raise SshHostTrustStoreError("Invalid SSH host trust store hosts")

        validated = []
        endpoints = set()
        for saved in payload["hosts"]:
            if not isinstance(saved, dict) or set(saved) != _HOST_FIELDS:
                raise SshHostTrustStoreError("Invalid SSH host trust entry")
            if not isinstance(saved["host"], str) or not saved["host"]:
                raise SshHostTrustStoreError("Invalid SSH host trust entry host")
            if (
                isinstance(saved["port"], bool)
                or not isinstance(saved["port"], int)
                or not 1 <= saved["port"] <= 65535
            ):
                raise SshHostTrustStoreError("Invalid SSH host trust entry port")
            if not isinstance(saved["key_type"], str) or not saved["key_type"]:
                raise SshHostTrustStoreError("Invalid SSH host trust entry key type")
            if (
                not isinstance(saved["fingerprint_sha256"], str)
                or not _FINGERPRINT_PATTERN.fullmatch(saved["fingerprint_sha256"])
            ):
                raise SshHostTrustStoreError("Invalid SSH host trust entry fingerprint")

            endpoint = (saved["host"], saved["port"])
            if endpoint in endpoints:
                raise SshHostTrustStoreError("Duplicate SSH host trust entry")
            endpoints.add(endpoint)
            validated.append(dict(saved))
        return validated

    def _write_hosts(self, hosts: List[Dict[str, object]]) -> None:
        if self._memory_hosts is not None:
            self._memory_hosts = [dict(host) for host in hosts]
            return

        payload = {
            "version": _SCHEMA_VERSION,
            "hosts": sorted(hosts, key=lambda item: (item["host"], item["port"])),
        }
        temp_path = None
        file_descriptor = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temp_name = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            with os.fdopen(
                file_descriptor, "w", encoding="utf-8", newline="\n"
            ) as trust_file:
                file_descriptor = None
                json.dump(
                    payload,
                    trust_file,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                trust_file.write("\n")
                trust_file.flush()
                os.fsync(trust_file.fileno())
            if os.name == "posix":
                os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
            temp_path = None
            _fsync_parent_directory(self.path.parent)
        except OSError as exc:
            raise SshHostTrustStoreError(
                "SSH host trust store could not be written"
            ) from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
