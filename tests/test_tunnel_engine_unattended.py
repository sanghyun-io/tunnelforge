"""Unattended (scheduled) tunnel start: no prompts, no auto-accept, no cached passphrases."""
import shutil
import subprocess
import threading
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from src.core import connection_trust as ct
from src.core import ssh_trust
from src.core.tunnel_engine import TunnelEngine
from tests.test_tunnel_engine_trust import MemoryStore


@pytest.fixture(autouse=True)
def _clean_registry():
    ct.clear_registered_tls()
    yield
    ct.clear_registered_tls()


@pytest.fixture
def config():
    return {
        'id': 't1', 'name': 'n', 'bastion_host': 'bastion.example', 'bastion_port': 22,
        'bastion_user': 'u', 'bastion_key': '/k', 'remote_host': 'db.internal',
        'remote_port': 3306, 'local_port': 13306, 'connection_mode': 'ssh_tunnel',
    }


@pytest.fixture
def host_key():
    return paramiko.ECDSAKey.generate()


def start(engine, config, host_key, unattended=True):
    with patch.object(ssh_trust, 'probe_host_key', return_value=host_key), \
            patch('src.core.tunnel_engine.SSHTunnelForwarder') as forwarder, \
            patch.object(engine, '_load_private_key', return_value=object()):
        method = engine.start_tunnel_unattended if unattended else engine.start_tunnel
        result = method(config, check_port=False)
    return result, forwarder


def test_unknown_host_key_is_never_auto_accepted_even_when_a_confirmer_would_say_yes(config, host_key):
    store = MemoryStore()
    engine = TunnelEngine(known_hosts=store)
    confirmer = MagicMock(return_value=True)
    engine.host_key_confirmer = confirmer
    (ok, message), forwarder = start(engine, config, host_key)
    assert not ok and "error_code=ssh_host_key_unknown" in message
    confirmer.assert_not_called()
    assert store.items == {}, "an unattended run must not record a new trust decision"
    forwarder.assert_not_called()


def test_interactive_start_still_asks_after_an_unattended_attempt(config, host_key):
    store = MemoryStore()
    engine = TunnelEngine(known_hosts=store)
    confirmer = MagicMock(return_value=True)
    engine.host_key_confirmer = confirmer
    start(engine, config, host_key)
    (ok, _), forwarder = start(engine, config, host_key, unattended=False)
    confirmer.assert_called_once()
    assert ssh_trust.host_id('bastion.example', 22) in store.items
    assert engine._is_unattended() is False


def test_trusted_host_key_works_unattended(config, host_key):
    store = MemoryStore()
    store.save_known_host('bastion.example', 22, ssh_trust.entry_for_key(host_key))
    engine = TunnelEngine(known_hosts=store)
    (ok, message), forwarder = start(engine, config, host_key)
    assert forwarder.called, message


def test_changed_host_key_is_blocked_unattended(config, host_key):
    store = MemoryStore()
    store.save_known_host('bastion.example', 22, ssh_trust.entry_for_key(paramiko.ECDSAKey.generate()))
    engine = TunnelEngine(known_hosts=store)
    (ok, message), forwarder = start(engine, config, host_key)
    assert not ok and "ssh_host_key_changed" in message
    forwarder.assert_not_called()


def test_flag_is_per_thread_and_is_reset_after_failures(config, host_key):
    engine = TunnelEngine(known_hosts=MemoryStore())
    seen = {}

    def worker():
        engine._unattended.active = True
        seen["other"] = engine._is_unattended()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen["other"] is True and engine._is_unattended() is False
    with patch.object(engine, "start_tunnel", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            engine.start_tunnel_unattended(config)
    assert engine._is_unattended() is False


@pytest.fixture
def encrypted_key(tmp_path):
    ssh_keygen = shutil.which('ssh-keygen')
    if not ssh_keygen:
        pytest.skip('ssh-keygen not available')
    path = tmp_path / 'id_ed25519'
    subprocess.run([ssh_keygen, '-q', '-t', 'ed25519', '-N', 'correct horse', '-f', str(path)], check=True)
    return str(path)


def test_encrypted_key_fails_unattended_even_with_a_provider_and_a_cached_passphrase(encrypted_key):
    engine = TunnelEngine(known_hosts=MemoryStore())
    provider = MagicMock(return_value='correct horse')
    engine.passphrase_provider = provider
    assert engine._load_private_key(encrypted_key) is not None  # interactive unlock caches the passphrase
    provider.reset_mock()
    engine._unattended.active = True
    try:
        with pytest.raises(ssh_trust.SshPassphraseRequired):
            engine._load_private_key(encrypted_key)
    finally:
        engine._unattended.active = False
    provider.assert_not_called()
