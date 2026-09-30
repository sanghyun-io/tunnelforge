"""TF-STATUS-110: SSH host-key trust-on-first-use."""
import paramiko
import pytest

from src.core import ssh_trust


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get_known_host(self, host, port):
        return self.items.get(ssh_trust.host_id(host, port))

    def save_known_host(self, host, port, entry):
        self.items[ssh_trust.host_id(host, port)] = entry


@pytest.fixture
def key():
    return paramiko.ECDSAKey.generate()


def test_fingerprint_is_openssh_sha256(key):
    fingerprint = ssh_trust.fingerprint_of(key)
    assert fingerprint.startswith("SHA256:")
    assert "=" not in fingerprint


def test_host_id_normalises_case_and_port():
    assert ssh_trust.host_id("Bastion.Example.COM", "22") == "bastion.example.com:22"


def test_unknown_key_needs_confirmation_and_is_not_saved_without_it(monkeypatch, key):
    monkeypatch.setattr(ssh_trust, "probe_host_key", lambda host, port, timeout=10: key)
    store = MemoryStore()
    with pytest.raises(ssh_trust.SshHostKeyUnknown) as info:
        ssh_trust.verify_host_key("h", 22, store, confirmer=None)
    assert info.value.code == "ssh_host_key_unknown"
    assert info.value.fingerprint == ssh_trust.fingerprint_of(key)
    assert "(error_code=ssh_host_key_unknown)" in str(info.value)
    assert store.items == {}

    with pytest.raises(ssh_trust.SshHostKeyUnknown):
        ssh_trust.verify_host_key("h", 22, store, confirmer=lambda prompt: False)
    assert store.items == {}


def test_confirmed_key_is_persisted_then_accepted_silently(monkeypatch, key):
    monkeypatch.setattr(ssh_trust, "probe_host_key", lambda host, port, timeout=10: key)
    store = MemoryStore()
    prompts = []
    verified = ssh_trust.verify_host_key("h", 22, store, confirmer=lambda p: prompts.append(p) or True)
    assert verified.asbytes() == key.asbytes()
    assert prompts[0].fingerprint == ssh_trust.fingerprint_of(key)
    assert store.items["h:22"]["fingerprint"] == ssh_trust.fingerprint_of(key)

    again = ssh_trust.verify_host_key("h", 22, store, confirmer=None)  # no prompt needed any more
    assert again.asbytes() == key.asbytes()


def test_changed_key_is_blocked_even_with_a_confirmer(monkeypatch, key):
    store = MemoryStore()
    monkeypatch.setattr(ssh_trust, "probe_host_key", lambda host, port, timeout=10: key)
    ssh_trust.verify_host_key("h", 22, store, confirmer=lambda p: True)

    new_key = paramiko.ECDSAKey.generate()
    monkeypatch.setattr(ssh_trust, "probe_host_key", lambda host, port, timeout=10: new_key)
    called = []
    with pytest.raises(ssh_trust.SshHostKeyChanged) as info:
        ssh_trust.verify_host_key("h", 22, store, confirmer=lambda p: called.append(p) or True)
    assert info.value.code == "ssh_host_key_changed"
    assert info.value.stored_fingerprint == ssh_trust.fingerprint_of(key)
    assert called == []  # a changed key is never re-trusted through the normal prompt
    assert store.items["h:22"]["fingerprint"] == ssh_trust.fingerprint_of(key)


def test_explicit_refresh_replaces_stored_key(monkeypatch, key):
    store = MemoryStore()
    monkeypatch.setattr(ssh_trust, "probe_host_key", lambda host, port, timeout=10: key)
    ssh_trust.verify_host_key("h", 22, store, confirmer=lambda p: True)

    new_key = paramiko.ECDSAKey.generate()
    monkeypatch.setattr(ssh_trust, "probe_host_key", lambda host, port, timeout=10: new_key)
    info = ssh_trust.refresh_host_key("h", 22, store)
    assert info.fingerprint == ssh_trust.fingerprint_of(new_key)
    assert ssh_trust.verify_host_key("h", 22, store, confirmer=None).asbytes() == new_key.asbytes()


def test_stored_entry_round_trips_to_a_pkey(key):
    entry = ssh_trust.entry_for_key(key)
    assert ssh_trust.decode_key(entry).asbytes() == key.asbytes()
