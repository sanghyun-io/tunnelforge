"""TF-STATUS-110: TLS policy resolution, endpoint registry and SSH host-key trust (TOFU)."""
import pytest

from src.core import connection_trust as ct
from src.core.db_core_facade import DbEndpoint


@pytest.fixture(autouse=True)
def _clean_registry():
    ct.clear_registered_tls()
    yield
    ct.clear_registered_tls()


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.9.9.9", "::1", "[::1]"])
def test_loopback_hosts(host):
    assert ct.is_loopback_host(host)


@pytest.mark.parametrize("host", ["db.example.com", "10.0.0.5", "192.168.1.1", "", "127.example.com", "0.0.0.0"])
def test_non_loopback_hosts(host):
    assert not ct.is_loopback_host(host)


def test_default_mode_for_new_profiles():
    assert ct.default_tls_mode("ssh_tunnel", "127.0.0.1") == "verify_full"  # tunnels are never exempt
    assert ct.default_tls_mode("direct", "localhost") == "disable"
    assert ct.default_tls_mode("direct", "::1") == "disable"
    assert ct.default_tls_mode("direct", "db.example.com") == "verify_full"


def test_legacy_profile_without_tls_key_stays_disabled():
    policy = ct.resolve_tls_policy({"connection_mode": "ssh_tunnel", "remote_host": "db.internal"})
    assert policy.mode == "disable"


def test_ssh_tunnel_sets_server_name_to_remote_host():
    policy = ct.resolve_tls_policy({
        "connection_mode": "ssh_tunnel", "remote_host": "db.internal",
        "db_tls_mode": "verify_full", "db_tls_ca_file": "ca.pem",
    })
    assert (policy.mode, policy.ca_file, policy.server_name) == ("verify_full", "ca.pem", "db.internal")


def test_direct_connection_has_no_server_name_override():
    policy = ct.resolve_tls_policy({
        "connection_mode": "direct", "remote_host": "db.example.com", "db_tls_mode": "verify_ca",
    })
    assert (policy.mode, policy.server_name) == ("verify_ca", "")


def test_unknown_mode_falls_back_to_disable():
    assert ct.resolve_tls_policy({"db_tls_mode": "require"}).mode == "disable"


@pytest.mark.parametrize("config,warns", [
    ({"connection_mode": "ssh_tunnel", "remote_host": "db.internal"}, True),  # legacy tunnel
    ({"connection_mode": "ssh_tunnel", "remote_host": "127.0.0.1", "db_tls_mode": "disable"}, True),  # tunnel never exempt
    ({"connection_mode": "direct", "remote_host": "db.example.com", "db_tls_mode": "disable"}, True),
    ({"connection_mode": "direct", "remote_host": "localhost", "db_tls_mode": "disable"}, False),
    ({"connection_mode": "direct", "remote_host": "127.0.0.1"}, False),
    ({"connection_mode": "ssh_tunnel", "remote_host": "db.internal", "db_tls_mode": "verify_full"}, False),
])
def test_insecure_connection_warning(config, warns):
    assert (ct.insecure_connection_warning(config) is not None) is warns


def test_registered_policy_flows_into_db_endpoint():
    ct.register_endpoint_tls("127.0.0.1", 13306, ct.TlsPolicy("verify_full", "ca.pem", "db.internal"))
    endpoint = DbEndpoint("mysql", "127.0.0.1", 13306, "u", "p", "d")
    assert endpoint.to_payload()["tls"] == {"mode": "verify_full", "ca_file": "ca.pem", "server_name": "db.internal"}
    other = DbEndpoint("mysql", "127.0.0.1", 13307, "u", "p", "d")
    assert "tls" not in other.to_payload()


def test_explicit_endpoint_tls_wins_over_registry():
    ct.register_endpoint_tls("127.0.0.1", 13306, ct.TlsPolicy("verify_full", "", "db.internal"))
    endpoint = DbEndpoint("mysql", "127.0.0.1", 13306, "u", "p", "d", tls_mode="disable")
    assert "tls" not in endpoint.to_payload()


def test_unregister_removes_policy():
    ct.register_endpoint_tls("127.0.0.1", 13306, ct.TlsPolicy("verify_ca"))
    ct.unregister_endpoint_tls("127.0.0.1", 13306)
    assert "tls" not in DbEndpoint("mysql", "127.0.0.1", 13306, "u", "p", "d").to_payload()


def test_extract_error_code():
    assert ct.extract_error_code("boom (error_code=tls_verification_failed)") == "tls_verification_failed"
    assert ct.extract_error_code("boom (error_code=ssh_host_key_changed)") == "ssh_host_key_changed"
    assert ct.extract_error_code("plain") is None
    assert ct.extract_error_code(None) is None


def test_friendly_message_is_korean_for_known_codes():
    for code in ("tls_verification_failed", "tls_unavailable", "ssh_host_key_unknown", "ssh_host_key_changed"):
        assert ct.friendly_error_message(code)
    assert ct.friendly_error_message("nope") is None


# ---- every DbEndpoint construction site picks up the registered policy -------------------

POLICY = ct.TlsPolicy("verify_full", "ca.pem", "db.internal")
EXPECTED = {"mode": "verify_full", "ca_file": "ca.pem", "server_name": "db.internal"}


class _CapturingFacade:
    def __init__(self):
        self.endpoints = []

    def open_connection(self, endpoint):
        self.endpoints.append(endpoint)
        return "cid"


def test_postgres_connector_uses_registered_policy():
    from src.core.postgres_connector import PostgresConnector

    ct.register_endpoint_tls("127.0.0.1", 15432, POLICY)
    facade = _CapturingFacade()
    assert PostgresConnector("127.0.0.1", 15432, "u", "p", "d", facade=facade).connect()[0]
    assert facade.endpoints[0].to_payload()["tls"] == EXPECTED


def test_dbapi_shim_and_factory_use_registered_policy():
    from src.core.db_core_dbapi_shim import RustDbConnector, create_rust_db_connector

    ct.register_endpoint_tls("127.0.0.1", 13306, POLICY)
    facade = _CapturingFacade()
    assert RustDbConnector("mysql", "127.0.0.1", 13306, "u", "p", "d", facade=facade).connect()[0]
    assert create_rust_db_connector("mysql", "127.0.0.1", 13306, "u", "p", facade=facade).endpoint.to_payload()["tls"] == EXPECTED
    assert facade.endpoints[0].to_payload()["tls"] == EXPECTED


def test_dump_exporter_endpoint_uses_registered_policy():
    from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

    ct.register_endpoint_tls("127.0.0.1", 13306, POLICY)
    config = RustDumpConfig(host="127.0.0.1", port=13306, user="u", password="p", engine="mysql")
    exporter = RustDumpExporter(config, facade=_CapturingFacade())
    assert exporter._endpoint("appdb").to_payload()["tls"] == EXPECTED


def test_facade_adds_friendly_hint_and_exposes_error_code():
    from src.core.db_core_client import DbCoreServiceError
    from src.core.db_core_facade import DbCoreFacade

    class Client:
        def request(self, command, payload=None):
            return {"success": False, "message": "boom (error_code=tls_verification_failed)",
                    "error_code": "tls_verification_failed"}

    facade = DbCoreFacade(Client())
    endpoint = DbEndpoint("mysql", "127.0.0.1", 1, "u", "p", "d")
    ok, message = facade.test_connection(endpoint)
    assert not ok and "인증서" in message and "boom" in message
    with pytest.raises(DbCoreServiceError) as info:
        facade.open_connection(endpoint)
    assert info.value.error_code == "tls_verification_failed" and "인증서" in str(info.value)


def test_strictest_policy_wins_for_shared_address():
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("verify_ca", "a.pem", ""), "p1")
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("verify_full", "b.pem", "db.internal"), "p2")
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("disable"), "p3")
    assert ct.lookup_endpoint_tls("h", 1) == ct.TlsPolicy("verify_full", "b.pem", "db.internal")
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("verify_ca", "c.pem", ""), "p1")
    assert ct.lookup_endpoint_tls("h", 1).mode == "verify_full"  # last registration does not win


def test_disable_never_weakens_a_registered_policy():
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("verify_full", "", "n"), "strict")
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("disable"), "legacy")
    assert ct.lookup_endpoint_tls("h", 1).mode == "verify_full"


def test_same_owner_can_downgrade_and_unregister_leaves_no_residue():
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("verify_full"), "p1")
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("disable"), "p1")  # user edited the profile
    assert ct.lookup_endpoint_tls("h", 1).mode == "disable"
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("verify_ca"), "p1")
    ct.register_endpoint_tls("h", 1, ct.TlsPolicy("verify_full"), "p2")
    ct.unregister_endpoint_tls("h", 1, "p2")
    assert ct.lookup_endpoint_tls("h", 1).mode == "verify_ca"
    ct.unregister_endpoint_tls("h", 1, "p1")
    assert ct.lookup_endpoint_tls("h", 1) == ct.TlsPolicy()
    assert ct._registry == {}
