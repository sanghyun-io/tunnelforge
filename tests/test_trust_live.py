"""Opt-in live check of SSH TOFU + DB TLS through a real tunnel (TF-STATUS-110).

Skipped unless TF_TLS_TEST_KEY_DIR is set. Environment comes from scripts/tls_live_env.sh:

    scripts/tls_live_env.sh certs && up-pg && cert pg good
    scripts/tls_live_env.sh up-ssh <plain key .pub> && authorize <encrypted key .pub>

    TF_TLS_TEST_KEY_DIR=<dir with k_plain, k_ed (passphrase secretpw)> \
    TF_TLS_TEST_CERT_DIR=<certs dir> TF_TLS_TEST_ENV_SCRIPT=scripts/tls_live_env.sh \
    TF_TLS_TEST_BASH=<git bash> pytest tests/test_trust_live.py -s
"""
import os
import subprocess

import pytest

from src.core import connection_trust as ct
from src.core import ssh_trust
from src.core.db_core_client import DbCoreServiceClient, DbCoreServiceError, db_core_executable
from src.core.db_core_facade import DbCoreFacade, DbEndpoint
from src.core.tunnel_engine import TunnelEngine

KEY_DIR = os.environ.get("TF_TLS_TEST_KEY_DIR")
pytestmark = pytest.mark.skipif(not KEY_DIR, reason="TF_TLS_TEST_KEY_DIR not set (opt-in live test)")


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get_known_host(self, host, port):
        return self.items.get(ssh_trust.host_id(host, port))

    def save_known_host(self, host, port, entry):
        self.items[ssh_trust.host_id(host, port)] = entry


def _rotate_ssh_host_key():
    bash = os.environ.get("TF_TLS_TEST_BASH", "bash")
    script = os.environ.get("TF_TLS_TEST_ENV_SCRIPT", "scripts/tls_live_env.sh")
    subprocess.run([bash, script, "rotate-ssh"], check=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})


def _profile(key, ca=True):
    profile = {
        "id": "live", "name": "live", "connection_mode": "ssh_tunnel",
        "bastion_host": "127.0.0.1", "bastion_port": int(os.environ.get("TF_TLS_TEST_SSH_PORT", "22222")),
        "bastion_user": "tfuser", "bastion_key": os.path.join(KEY_DIR, key),
        "remote_host": "tf-db.test", "remote_port": 5432, "local_port": 25999,
        "db_tls_mode": "verify_full",
    }
    if ca:
        profile["db_tls_ca_file"] = os.path.join(os.environ["TF_TLS_TEST_CERT_DIR"], "ca.pem")
    return profile


def _db_connect(engine, tunnel_id="live"):
    host, port = engine.get_connection_info(tunnel_id)
    facade = DbCoreFacade(DbCoreServiceClient(executable=os.environ.get("TF_TLS_TEST_CORE") or db_core_executable()))
    endpoint = DbEndpoint("postgresql", host, port, "postgres", "tfpass", "postgres")
    try:
        return endpoint, facade.open_connection(endpoint)
    finally:
        facade.client.shutdown()


@pytest.fixture(autouse=True)
def _clean_registry():
    ct.clear_registered_tls()
    yield
    ct.clear_registered_tls()


def test_ssh_tofu_lifecycle_and_db_tls_through_tunnel():
    store = MemoryStore()
    engine = TunnelEngine(known_hosts=store)
    profile = _profile("k_plain")

    # 1. first contact without a confirmer is refused
    ok, message = engine.start_tunnel(profile, check_port=False)
    assert not ok and ct.extract_error_code(message) == "ssh_host_key_unknown", message
    assert store.items == {}
    print("PASS ssh first contact blocked: ssh_host_key_unknown")

    # 2. user confirms the fingerprint -> saved, tunnel up, DB TLS verified with server_name=remote_host
    seen = []
    engine.host_key_confirmer = lambda prompt: seen.append(prompt) or True
    ok, message = engine.start_tunnel(profile, check_port=False)
    assert ok, message
    assert seen and seen[0].fingerprint.startswith("SHA256:")
    assert list(store.items.values())[0]["fingerprint"] == seen[0].fingerprint
    endpoint, connection_id = _db_connect(engine)
    assert endpoint.to_payload()["tls"]["server_name"] == "tf-db.test"
    assert connection_id
    print("PASS ssh confirm+save; PG verify_full via tunnel connected, server_name auto=tf-db.test")
    engine.stop_tunnel("live")

    # 3. same tunnel, DB CA not trusted -> TLS verification error code surfaces through the facade
    no_ca = _profile("k_plain", ca=False)
    ok, message = engine.start_tunnel(no_ca, check_port=False)
    assert ok, message
    with pytest.raises(DbCoreServiceError) as info:
        _db_connect(engine)
    assert info.value.error_code == "tls_verification_failed"
    print("PASS untrusted CA blocked through tunnel: tls_verification_failed")
    engine.stop_tunnel("live")

    # 4. known key + no prompt needed
    engine.host_key_confirmer = None
    ok, message = engine.start_tunnel(profile, check_port=False)
    assert ok, message
    engine.stop_tunnel("live")
    print("PASS known host key accepted without prompt")

    # 5. server key rotated -> blocked; explicit refresh -> allowed
    _rotate_ssh_host_key()
    ok, message = engine.start_tunnel(profile, check_port=False)
    assert not ok and ct.extract_error_code(message) == "ssh_host_key_changed", message
    temp_ok, _, temp_message = engine.create_temp_tunnel(profile)
    assert not temp_ok and ct.extract_error_code(temp_message) == "ssh_host_key_changed"
    reach_ok, reach_message = engine.test_target_reachable_from_bastion(profile)
    assert not reach_ok and ct.extract_error_code(reach_message) == "ssh_host_key_changed"
    print("PASS rotated host key blocked on forwarder, temp tunnel and paramiko probe: ssh_host_key_changed")
    old_fingerprint = list(store.items.values())[0]["fingerprint"]
    refreshed = engine.refresh_host_key(profile["bastion_host"], profile["bastion_port"])
    assert refreshed.fingerprint != old_fingerprint
    ok, message = engine.start_tunnel(profile, check_port=False)
    assert ok, message
    engine.stop_tunnel("live")
    print("PASS explicit key refresh, then connect succeeds")


def test_encrypted_private_key_login():
    engine = TunnelEngine(known_hosts=MemoryStore())
    engine.host_key_confirmer = lambda prompt: True
    profile = _profile("k_ed")

    ok, message = engine.start_tunnel(profile, check_port=False)
    assert not ok and "비밀번호" in message, message
    print("PASS encrypted key without passphrase provider refused")

    engine.passphrase_provider = lambda path, retry: "secretpw"
    ok, message = engine.start_tunnel(profile, check_port=False)
    assert ok, message
    engine.stop_tunnel("live")
    print("PASS encrypted key login with passphrase (memory only)")

    wrong = TunnelEngine(known_hosts=MemoryStore())
    wrong.host_key_confirmer = lambda prompt: True
    wrong.passphrase_provider = lambda path, retry: "not-it"
    ok, message = wrong.start_tunnel(profile, check_port=False)
    assert not ok and "올바르지 않" in message, message
    print("PASS wrong passphrase rejected")
