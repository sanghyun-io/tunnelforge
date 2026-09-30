"""TF-STATUS-110: TunnelEngine host-key pinning, encrypted keys and DB TLS registration."""
import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from src.core import connection_trust as ct
from src.core import ssh_trust
from src.core.tunnel_engine import TunnelEngine


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get_known_host(self, host, port):
        return self.items.get(ssh_trust.host_id(host, port))

    def save_known_host(self, host, port, entry):
        self.items[ssh_trust.host_id(host, port)] = entry


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
        'db_tls_mode': 'verify_full',
    }


@pytest.fixture
def host_key():
    return paramiko.ECDSAKey.generate()


def test_forwarder_is_pinned_to_the_verified_host_key(config, host_key):
    engine = TunnelEngine(known_hosts=MemoryStore())
    engine.host_key_confirmer = lambda prompt: True
    with patch.object(ssh_trust, 'probe_host_key', return_value=host_key), \
            patch('src.core.tunnel_engine.SSHTunnelForwarder') as forwarder:
        engine._build_forwarder(config, ('127.0.0.1', 0), object())
    assert forwarder.call_args.kwargs['ssh_host_key'].asbytes() == host_key.asbytes()


def test_unknown_host_without_confirmer_blocks_tunnel(config, host_key):
    engine = TunnelEngine(known_hosts=MemoryStore())
    with patch.object(ssh_trust, 'probe_host_key', return_value=host_key), \
            patch('src.core.tunnel_engine.SSHTunnelForwarder') as forwarder, \
            patch.object(engine, '_load_private_key', return_value=object()):
        ok, message = engine.start_tunnel(config, check_port=False)
    assert not ok
    assert 'error_code=ssh_host_key_unknown' in message
    forwarder.assert_not_called()


def test_changed_host_key_blocks_tunnel_and_temp_tunnel(config, host_key):
    store = MemoryStore()
    store.save_known_host('bastion.example', 22, ssh_trust.entry_for_key(paramiko.ECDSAKey.generate()))
    engine = TunnelEngine(known_hosts=store)
    engine.host_key_confirmer = lambda prompt: True
    with patch.object(ssh_trust, 'probe_host_key', return_value=host_key), \
            patch('src.core.tunnel_engine.SSHTunnelForwarder') as forwarder, \
            patch.object(engine, '_load_private_key', return_value=object()):
        ok, message = engine.start_tunnel(config, check_port=False)
        temp_ok, _, temp_message = engine.create_temp_tunnel(config)
        reach_ok, reach_message = engine.test_target_reachable_from_bastion(config)
    assert not ok and 'error_code=ssh_host_key_changed' in message
    assert not temp_ok and 'error_code=ssh_host_key_changed' in temp_message
    assert not reach_ok and 'error_code=ssh_host_key_changed' in reach_message
    forwarder.assert_not_called()


def test_paramiko_probe_path_rejects_unknown_keys(config, host_key):
    store = MemoryStore()
    store.save_known_host('bastion.example', 22, ssh_trust.entry_for_key(host_key))
    engine = TunnelEngine(known_hosts=store)
    with patch.object(ssh_trust, 'probe_host_key', return_value=host_key), \
            patch.object(engine, '_load_private_key', return_value=object()), \
            patch('src.core.tunnel_engine.paramiko.SSHClient') as client_cls:
        engine.test_target_reachable_from_bastion(config)
    client = client_cls.return_value
    assert isinstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
    client.get_host_keys.return_value.add.assert_called_once_with('bastion.example', host_key.get_name(), host_key)


def test_bad_host_key_handshake_error_is_tagged_changed(config, host_key):
    store = MemoryStore()
    store.save_known_host('bastion.example', 22, ssh_trust.entry_for_key(host_key))
    engine = TunnelEngine(known_hosts=store)
    forwarder = MagicMock()
    forwarder.start.side_effect = paramiko.SSHException('Bad host key from server')
    with patch.object(ssh_trust, 'probe_host_key', return_value=host_key), \
            patch('src.core.tunnel_engine.SSHTunnelForwarder', return_value=forwarder), \
            patch.object(engine, '_load_private_key', return_value=object()):
        ok, message = engine.start_tunnel(config, check_port=False)
    assert not ok and 'error_code=ssh_host_key_changed' in message


def test_tunnel_registers_and_releases_db_tls_policy(config, host_key):
    store = MemoryStore()
    store.save_known_host('bastion.example', 22, ssh_trust.entry_for_key(host_key))
    engine = TunnelEngine(known_hosts=store)
    server = MagicMock(is_active=True, local_bind_port=13306)
    with patch.object(ssh_trust, 'probe_host_key', return_value=host_key), \
            patch('src.core.tunnel_engine.SSHTunnelForwarder', return_value=server), \
            patch.object(engine, '_load_private_key', return_value=object()):
        assert engine.start_tunnel(config, check_port=False)[0]
    assert ct.lookup_endpoint_tls('127.0.0.1', 13306) == ct.TlsPolicy('verify_full', '', 'db.internal')
    engine.stop_tunnel('t1')
    assert ct.lookup_endpoint_tls('127.0.0.1', 13306).mode == 'disable'


def test_direct_connection_registers_policy_without_server_name():
    engine = TunnelEngine(known_hosts=MemoryStore())
    direct = {'id': 'd1', 'name': 'd', 'connection_mode': 'direct', 'remote_host': 'db.example.com',
              'remote_port': 3306, 'db_tls_mode': 'verify_ca', 'db_tls_ca_file': 'ca.pem'}
    assert engine.start_tunnel(direct)[0]
    assert ct.lookup_endpoint_tls('db.example.com', 3306) == ct.TlsPolicy('verify_ca', 'ca.pem', '')
    engine.stop_tunnel('d1')
    assert ct.lookup_endpoint_tls('db.example.com', 3306).mode == 'disable'


def test_legacy_profile_registers_nothing():
    engine = TunnelEngine(known_hosts=MemoryStore())
    direct = {'id': 'd1', 'name': 'd', 'connection_mode': 'direct', 'remote_host': 'db.example.com',
              'remote_port': 3306}
    assert engine.start_tunnel(direct)[0]
    assert ct.lookup_endpoint_tls('db.example.com', 3306).mode == 'disable'


# ---- encrypted private keys -------------------------------------------------------------

@pytest.fixture
def encrypted_key(tmp_path):
    ssh_keygen = shutil.which('ssh-keygen')
    if not ssh_keygen:
        pytest.skip('ssh-keygen not available')
    path = tmp_path / 'id_ed25519'
    subprocess.run([ssh_keygen, '-q', '-t', 'ed25519', '-N', 'correct horse', '-f', str(path)], check=True)
    return str(path)


def test_encrypted_key_without_provider_reports_passphrase_required(encrypted_key):
    with pytest.raises(ssh_trust.SshPassphraseRequired):
        TunnelEngine(known_hosts=MemoryStore())._load_private_key(encrypted_key)


def test_encrypted_key_passphrase_is_kept_in_memory_only(encrypted_key, tmp_path):
    engine = TunnelEngine(known_hosts=MemoryStore())
    asked = []
    engine.passphrase_provider = lambda path, retry: asked.append(retry) or 'correct horse'
    assert engine._load_private_key(encrypted_key) is not None
    assert engine._load_private_key(encrypted_key) is not None  # cached: no second prompt
    assert asked == [False]
    for root, _, files in os.walk(tmp_path):
        for name in files:
            with open(os.path.join(root, name), 'rb') as handle:
                assert b'correct horse' not in handle.read()


def test_wrong_passphrase_is_retried_then_rejected(encrypted_key):
    engine = TunnelEngine(known_hosts=MemoryStore())
    answers = iter(['bad-1', 'bad-2', 'bad-3'])
    engine.passphrase_provider = lambda path, retry: next(answers)
    with pytest.raises(ssh_trust.SshPassphraseInvalid):
        engine._load_private_key(encrypted_key)


def test_cancelled_passphrase_prompt_reports_required(encrypted_key):
    engine = TunnelEngine(known_hosts=MemoryStore())
    engine.passphrase_provider = lambda path, retry: None
    with pytest.raises(ssh_trust.SshPassphraseRequired):
        engine._load_private_key(encrypted_key)
