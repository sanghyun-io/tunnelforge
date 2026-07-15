import json
import os
import re
import stat

import paramiko
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from src.core.ssh_host_trust import (
    SshHostKeyChangedError,
    SshHostKeyTrustStore,
    SshHostTrustStoreError,
)


def _ed25519_public_key():
    public_bytes = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    message = paramiko.Message()
    message.add_string("ssh-ed25519")
    message.add_string(public_bytes)
    return paramiko.Ed25519Key(data=message.asbytes())


@pytest.fixture
def ed25519_key():
    return _ed25519_public_key()


@pytest.fixture
def first_key():
    return _ed25519_public_key()


@pytest.fixture
def second_key():
    return _ed25519_public_key()


def test_fingerprint_uses_openssh_sha256_without_padding(ed25519_key):
    check = SshHostKeyTrustStore.in_memory().check(
        "bastion.example", 22, ed25519_key
    )

    assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", check.fingerprint_sha256)
    assert "=" not in check.fingerprint_sha256


def test_approval_persists_only_public_identity(tmp_path, ed25519_key):
    path = tmp_path / "ssh_host_trust.json"
    store = SshHostKeyTrustStore(path)

    check = store.check("bastion.example", 2222, ed25519_key)
    store.approve(check)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "hosts": [
            {
                "host": "bastion.example",
                "port": 2222,
                "key_type": "ssh-ed25519",
                "fingerprint_sha256": check.fingerprint_sha256,
            }
        ],
    }
    assert ed25519_key.get_base64() not in path.read_text(encoding="utf-8")


def test_changed_key_cannot_be_approved(tmp_path, first_key, second_key):
    store = SshHostKeyTrustStore(tmp_path / "ssh_host_trust.json")
    original = store.check("bastion.example", 22, first_key)
    store.approve(original)

    changed = store.check("bastion.example", 22, second_key)

    assert changed.status == "changed"
    assert changed.previous_fingerprint_sha256 == original.fingerprint_sha256
    with pytest.raises(SshHostKeyChangedError):
        store.approve(changed)


def test_host_and_port_are_independent_trust_identities(tmp_path, ed25519_key):
    store = SshHostKeyTrustStore(tmp_path / "ssh_host_trust.json")
    store.approve(store.check("bastion.example", 22, ed25519_key))

    assert store.check("bastion.example", 22, ed25519_key).status == "trusted"
    assert store.check("bastion.example", 2222, ed25519_key).status == "approval_required"
    assert store.check("other.example", 22, ed25519_key).status == "approval_required"


def test_algorithm_change_is_a_changed_host_key(tmp_path, ed25519_key):
    store = SshHostKeyTrustStore(tmp_path / "ssh_host_trust.json")
    original = store.check("bastion.example", 22, ed25519_key)
    store.approve(original)

    changed = store.check("bastion.example", 22, paramiko.RSAKey.generate(1024))

    assert changed.status == "changed"
    assert changed.key_type == "ssh-rsa"
    assert changed.previous_fingerprint_sha256 == original.fingerprint_sha256


def test_corrupted_json_fails_closed(tmp_path, ed25519_key):
    path = tmp_path / "ssh_host_trust.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(SshHostTrustStoreError):
        SshHostKeyTrustStore(path).check("bastion.example", 22, ed25519_key)


def test_unsupported_schema_version_fails_closed(tmp_path, ed25519_key):
    path = tmp_path / "ssh_host_trust.json"
    path.write_text(json.dumps({"version": 2, "hosts": []}), encoding="utf-8")

    with pytest.raises(SshHostTrustStoreError):
        SshHostKeyTrustStore(path).check("bastion.example", 22, ed25519_key)


def test_atomic_replace_failure_cleans_up_temporary_file(
    tmp_path, first_key, second_key, monkeypatch
):
    path = tmp_path / "ssh_host_trust.json"
    store = SshHostKeyTrustStore(path)
    store.approve(store.check("first.example", 22, first_key))
    original_contents = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("src.core.ssh_host_trust.os.replace", fail_replace)
    with pytest.raises(SshHostTrustStoreError):
        store.approve(store.check("second.example", 22, second_key))

    assert path.read_bytes() == original_contents
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_trust_file_is_user_only_on_posix(tmp_path, ed25519_key):
    path = tmp_path / "ssh_host_trust.json"
    store = SshHostKeyTrustStore(path)

    store.approve(store.check("bastion.example", 22, ed25519_key))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
